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
        self.consecutive_probe_successes = 0
        self.last_probe_at = 0.0
        self.last_probe_duration_seconds = 0.0
        self.last_failure_at = 0.0
        self.last_failure = ""
        self.recovery_stable_since = 0.0

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

    def record_probe(self, success: bool, duration_seconds: float, error: str = "") -> None:
        with self._lock:
            self.last_probe_at = time.time()
            self.last_probe_duration_seconds = duration_seconds
            if success:
                self.consecutive_probe_successes += 1
            else:
                self.consecutive_probe_successes = 0
                self.last_failure_at = self.last_probe_at
                self.last_failure = error

    def set_recovery_stable_since(self, value: float) -> None:
        with self._lock:
            self.recovery_stable_since = value

    def increment_failure(self) -> int:
        with self._lock:
            self.failures_total += 1
            self.last_failure_at = time.time()
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
                "consecutive_probe_successes": self.consecutive_probe_successes,
                "last_probe_at": self.last_probe_at,
                "last_probe_duration_seconds": self.last_probe_duration_seconds,
                "last_failure_at": self.last_failure_at,
                "last_failure": self.last_failure,
                "recovery_stable_since": self.recovery_stable_since,
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
            return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    @staticmethod
    def _label(value: object) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def _metrics(self, snapshot: dict[str, Any]) -> None:
        lines = [
            "# HELP s3_storage_node_ready Whether the appliance can safely serve S3 traffic.",
            "# TYPE s3_storage_node_ready gauge",
            f"s3_storage_node_ready {1 if snapshot['ready'] else 0}",
            "# TYPE s3_storage_node_failures_total counter",
            f"s3_storage_node_failures_total {snapshot['failures_total']}",
            "# TYPE s3_storage_node_recovery_attempts counter",
            f"s3_storage_node_recovery_attempts {snapshot['recovery_attempts']}",
            "# TYPE s3_storage_node_consecutive_probe_successes gauge",
            f"s3_storage_node_consecutive_probe_successes {snapshot['consecutive_probe_successes']}",
            "# TYPE s3_storage_node_last_probe_duration_seconds gauge",
            f"s3_storage_node_last_probe_duration_seconds {snapshot['last_probe_duration_seconds']}",
        ]
        for name, values in snapshot["storage"].items():
            target = self._label(name)
            if "free_bytes" in values:
                lines.append(f's3_storage_node_storage_free_bytes{{target="{target}"}} {values["free_bytes"]}')
            if "total_bytes" in values:
                lines.append(f's3_storage_node_storage_total_bytes{{target="{target}"}} {values["total_bytes"]}')
            if "connected_channels" in values:
                lines.append(
                    f's3_storage_node_cifs_connected_channels{{target="{target}"}} {values["connected_channels"]}'
                )
            if "cifs_session_reconnects" in values:
                lines.append(
                    f's3_storage_node_cifs_session_reconnects{{target="{target}"}} {values["cifs_session_reconnects"]}'
                )
            if "cifs_share_reconnects" in values:
                lines.append(
                    f's3_storage_node_cifs_share_reconnects{{target="{target}"}} {values["cifs_share_reconnects"]}'
                )
            if "effective_smb_dialect" in values:
                dialect = self._label(values["effective_smb_dialect"])
                handles = self._label(values.get("effective_handle_reconnect_mode", "disabled"))
                lines.append(
                    f's3_storage_node_cifs_transport_info{{target="{target}",dialect="{dialect}",handles="{handles}"}} 1'
                )
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
