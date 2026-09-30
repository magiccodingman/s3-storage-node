"""Disposable HAProxy integration check; no production endpoints or storage."""
from __future__ import annotations

import csv
import http.client
import io
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as S

from s3_storage_node.admission import AdmissionController, runtime_command, write_stats
from s3_storage_node.config_types import S3AdmissionConfig
from s3_storage_node.health import HealthState, start_server
from s3_storage_node.render import render_haproxy


def wait_for(predicate, seconds=5):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("integration condition did not become true")


def main():
    gate = threading.Event()
    entered = threading.Event()
    order = []
    active = 0
    maximum = 0
    lock = threading.Lock()

    class Worker(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                order.append(self.path)
            try:
                if self.path == "/hold":
                    entered.set()
                    assert gate.wait(10)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            finally:
                with lock:
                    active -= 1

    worker = ThreadingHTTPServer(("127.0.0.1", 0), Worker)
    threading.Thread(target=worker.serve_forever, daemon=True).start()
    health = HealthState()
    health.set("ONLINE", True)
    health.record_probe(True, 0.01)
    health_server = start_server("127.0.0.1", 0, health)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    def request(path):
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
        try:
            client.request("PUT", path, body=b"")
            response = client.getresponse()
            response.read()
            return response.status, response.getheader("Retry-After")
        finally:
            client.close()
    try:
        with tempfile.TemporaryDirectory(prefix="s3-admission-") as directory:
            config = S(
                appliance=S(runtime_dir=Path(directory), health_host="127.0.0.1",
                            health_port=health_server.server_port, probe_interval_seconds=5, probe_timeout_seconds=15),
                s3=S(host="127.0.0.1", port=port, tls_mode="off", admission=S3AdmissionConfig()),
                seaweed=S(s3_internal_port=worker.server_port), worker_endpoint_host="127.0.0.1",
            )
            path = render_haproxy(config)
            subprocess.run(["/usr/sbin/haproxy", "-c", "-f", str(path)], check=True)
            proxy = subprocess.Popen(["/usr/sbin/haproxy", "-db", "-f", str(path)], stdout=subprocess.DEVNULL)
            try:
                sock = config.appliance.runtime_dir / "admission.sock"
                wait_for(sock.exists)
                def backend_ready():
                    rows = csv.DictReader(io.StringIO(runtime_command(sock, "show stat").removeprefix("# ")))
                    return any(r.get("pxname") == "seaweed_s3_write" and r.get("svname") == "worker_s3_write" and r.get("status") == "UP" for r in rows)
                wait_for(backend_ready)
                with ThreadPoolExecutor(max_workers=6) as clients:
                    first = clients.submit(request, "/hold")
                    assert entered.wait(5)
                    bulk = clients.submit(request, "/bucket/harbor/blob")
                    wait_for(lambda: write_stats(runtime_command(sock, "show stat"))[0] == 1)
                    small = clients.submit(request, "/bucket/small-backup")
                    wait_for(lambda: write_stats(runtime_command(sock, "show stat"))[0] == 2)
                    extra = [clients.submit(request, f"/bucket/harbor/extra{i}") for i in range(2)]
                    wait_for(lambda: write_stats(runtime_command(sock, "show stat"))[0] == 4)
                    assert request("/overflow") == (503, "3")
                    gate.set()
                    assert all(f.result()[0] == 200 for f in [first, bulk, small, *extra])
                assert maximum == 1, maximum
                assert order[:2] == ["/hold", "/bucket/small-backup"], order
                controller = AdmissionController(config, health, lambda: False)
                controller.budget.healthy_since = time.monotonic() - 301
                health.record_probe(True, 0.01)
                controller.tick()
                assert health.snapshot()["admission"]["applied"]
                def server_limit():
                    rows = csv.DictReader(io.StringIO(runtime_command(sock, "show stat").removeprefix("# ")))
                    return next(int(r["slim"]) for r in rows if r.get("pxname") == "seaweed_s3_write" and r.get("svname") == "worker_s3_write")
                assert server_limit() == 2
                health.record_probe(True, 2.1)
                controller.tick()
                assert server_limit() == 1
                print("PASS: shared write cap, bounded queue, Retry-After, small-operation priority, adaptive ramp/backoff")
            finally:
                gate.set()
                proxy.terminate()
                proxy.wait(timeout=5)
    finally:
        worker.shutdown()
        health_server.shutdown()


if __name__ == "__main__":
    main()
