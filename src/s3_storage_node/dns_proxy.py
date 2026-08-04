from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import socket
import struct
import threading
from pathlib import Path


class DnsProxyError(RuntimeError):
    pass


def _socket_address(address: str, port: int) -> tuple[int, tuple[object, ...]]:
    parsed = ipaddress.ip_address(address)
    if parsed.version == 4:
        return socket.AF_INET, (address, port)
    return socket.AF_INET6, (address, port, 0, 0)


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise DnsProxyError("DNS TCP peer closed before the complete message arrived")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class DnsProxy:
    """Transparent DNS relay between a worker veth and root-namespace resolvers."""

    def __init__(
        self,
        listen_address: str,
        upstreams: tuple[str, ...],
        *,
        port: int = 53,
        timeout_seconds: float = 2.0,
        ready_file: Path | None = None,
    ) -> None:
        if not upstreams:
            raise DnsProxyError("at least one upstream DNS resolver is required")
        ipaddress.ip_address(listen_address)
        for upstream in upstreams:
            ipaddress.ip_address(upstream)
        if not 1 <= port <= 65535:
            raise DnsProxyError("DNS port must be between 1 and 65535")
        self.listen_address = listen_address
        self.upstreams = upstreams
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.ready_file = ready_file
        self.stop_event = threading.Event()
        self.udp_socket: socket.socket | None = None
        self.tcp_socket: socket.socket | None = None

    def serve(self) -> None:
        family, bind_address = _socket_address(self.listen_address, self.port)
        udp = socket.socket(family, socket.SOCK_DGRAM)
        tcp = socket.socket(family, socket.SOCK_STREAM)
        for sock in (udp, tcp):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(bind_address)
            sock.settimeout(0.5)
        tcp.listen(32)
        self.udp_socket = udp
        self.tcp_socket = tcp
        self._publish_ready()

        udp_thread = threading.Thread(target=self._udp_loop, name="dns-udp", daemon=True)
        tcp_thread = threading.Thread(target=self._tcp_loop, name="dns-tcp", daemon=True)
        udp_thread.start()
        tcp_thread.start()
        self.stop_event.wait()
        self.close()
        udp_thread.join(timeout=1)
        tcp_thread.join(timeout=1)

    def stop(self) -> None:
        self.stop_event.set()

    def close(self) -> None:
        for sock in (self.udp_socket, self.tcp_socket):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.udp_socket = None
        self.tcp_socket = None
        if self.ready_file is not None:
            try:
                self.ready_file.unlink()
            except FileNotFoundError:
                pass

    def _publish_ready(self) -> None:
        if self.ready_file is None:
            return
        self.ready_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.ready_file.with_suffix(self.ready_file.suffix + ".tmp")
        with open(temporary, "w", encoding="ascii") as handle:
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.ready_file)

    def _udp_loop(self) -> None:
        assert self.udp_socket is not None
        while not self.stop_event.is_set():
            try:
                query, client = self.udp_socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle_udp,
                args=(query, client),
                name="dns-udp-query",
                daemon=True,
            ).start()

    def _handle_udp(self, query: bytes, client: tuple[object, ...]) -> None:
        response = self._forward_udp(query)
        if response is None or self.udp_socket is None:
            return
        try:
            self.udp_socket.sendto(response, client)
        except OSError:
            return

    def _forward_udp(self, query: bytes) -> bytes | None:
        for upstream in self.upstreams:
            family, endpoint = _socket_address(upstream, 53)
            try:
                with socket.socket(family, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(self.timeout_seconds)
                    sock.sendto(query, endpoint)
                    response, _ = sock.recvfrom(65535)
                    return response
            except OSError:
                continue
        return None

    def _tcp_loop(self) -> None:
        assert self.tcp_socket is not None
        while not self.stop_event.is_set():
            try:
                connection, _ = self.tcp_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle_tcp,
                args=(connection,),
                name="dns-tcp-query",
                daemon=True,
            ).start()

    def _handle_tcp(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(self.timeout_seconds * max(1, len(self.upstreams)))
            try:
                length = struct.unpack("!H", _recv_exact(connection, 2))[0]
                query = _recv_exact(connection, length)
            except (OSError, DnsProxyError, struct.error):
                return
            response = self._forward_tcp(query)
            if response is None:
                return
            try:
                connection.sendall(struct.pack("!H", len(response)) + response)
            except OSError:
                return

    def _forward_tcp(self, query: bytes) -> bytes | None:
        framed = struct.pack("!H", len(query)) + query
        for upstream in self.upstreams:
            family, endpoint = _socket_address(upstream, 53)
            try:
                with socket.socket(family, socket.SOCK_STREAM) as sock:
                    sock.settimeout(self.timeout_seconds)
                    sock.connect(endpoint)
                    sock.sendall(framed)
                    response_length = struct.unpack("!H", _recv_exact(sock, 2))[0]
                    return _recv_exact(sock, response_length)
            except (OSError, DnsProxyError, struct.error):
                continue
        return None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="s3-storage-node-dns-proxy")
    result.add_argument("--listen-address", required=True)
    result.add_argument("--upstream", action="append", required=True)
    result.add_argument("--port", type=int, default=53)
    result.add_argument("--timeout-seconds", type=float, default=2.0)
    result.add_argument("--ready-file", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        proxy = DnsProxy(
            args.listen_address,
            tuple(args.upstream),
            port=args.port,
            timeout_seconds=args.timeout_seconds,
            ready_file=args.ready_file,
        )
    except (DnsProxyError, ValueError) as exc:
        print(str(exc), file=os.sys.stderr)
        return 2

    def stop(_signum: int, _frame: object) -> None:
        proxy.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        proxy.serve()
    except OSError as exc:
        print(f"DNS proxy failed: {exc}", file=os.sys.stderr)
        proxy.close()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
