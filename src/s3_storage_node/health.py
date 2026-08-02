from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class HealthState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "BOOTSTRAPPING"
        self.ready = False
        self.reason = "starting"
        self.since = time.time()
        self.storage: dict[str, dict[str, Any]] = {}
        self.recovery_attempts = 0
        self.failures_total = 0

    def set(self, state: str, ready: bool, reason: str = "") -> None:
        with self._lock:
            if state != self.state:
                self.since = time.time()
            self.state = state
            self.ready = ready
            self.reason = reason

    def set_storage(self, name: str, values: dict[str, Any]) -> None:
        with self._lock:
            self.storage[name] = values

    def increment_failure(self) -> int:
        with self._lock:
            self.failures_total += 1
            return self.failures_total

    def increment_recovery(self) -> int:
        with self._lock:
            self.recovery_attempts += 1
            return self.recovery_attempts

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "ready": self.ready,
                "reason": self.reason,
                "since": self.since,
                "storage": self.storage,
                "recovery_attempts": self.recovery_attempts,
                "failures_total": self.failures_total,
            }


class Handler(BaseHTTPRequestHandler):
    state: HealthState

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        snapshot = self.state.snapshot()
        if self.path == "/live":
            self._json(200, {"live": True, **snapshot})
        elif self.path == "/ready":
            self._json(200 if snapshot["ready"] else 503, snapshot)
        elif self.path == "/healthz":
            self._json(200 if snapshot["ready"] else 503, snapshot)
        elif self.path == "/metrics":
            self._metrics(snapshot)
        else:
            self._json(404, {"error": "not found"})

    def _write(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Health clients such as HAProxy may close immediately after reading
            # the status line. This is expected and should not flood appliance logs.
            return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def _metrics(self, snapshot: dict[str, Any]) -> None:
        lines = [
            "# HELP s3_storage_node_ready Whether the appliance can safely serve S3 traffic.",
            "# TYPE s3_storage_node_ready gauge",
            f"s3_storage_node_ready {1 if snapshot['ready'] else 0}",
            "# TYPE s3_storage_node_failures_total counter",
            f"s3_storage_node_failures_total {snapshot['failures_total']}",
            "# TYPE s3_storage_node_recovery_attempts counter",
            f"s3_storage_node_recovery_attempts {snapshot['recovery_attempts']}",
        ]
        for name, values in snapshot["storage"].items():
            if "free_bytes" in values:
                lines.append(f's3_storage_node_storage_free_bytes{{target="{name}"}} {values["free_bytes"]}')
            if "total_bytes" in values:
                lines.append(f's3_storage_node_storage_total_bytes{{target="{name}"}} {values["total_bytes"]}')
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)


def start_server(host: str, port: int, state: HealthState) -> ThreadingHTTPServer:
    handler = type("BoundHealthHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    return server
