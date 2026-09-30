"""Conservative, non-blocking feedback control of HAProxy's shared write budget."""
from __future__ import annotations

import csv
import io
import socket
import threading
import time
from pathlib import Path

from .logging import event


class WriteBudget:
    def __init__(self, ceiling: int, healthy_window: int) -> None:
        self.ceiling = ceiling
        self.healthy_window = healthy_window
        self.limit = 1
        self.healthy_since: float | None = None
        self.backoffs = 0

    def update(self, now: float, healthy: bool) -> int:
        if not healthy:
            if self.limit > 1:
                self.backoffs += 1
            self.limit = 1
            self.healthy_since = None
        elif self.healthy_since is None:
            self.healthy_since = now
        elif now - self.healthy_since >= self.healthy_window:
            self.limit = min(self.ceiling, self.limit + 1)
            self.healthy_since = now
        return self.limit


def runtime_command(path: Path, command: str) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        client.connect(str(path))
        client.sendall((command + "\n").encode())
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        size = 0
        while chunk := client.recv(65536):
            size += len(chunk)
            if size > 1024 * 1024:
                raise OSError("HAProxy runtime response exceeds limit")
            chunks.append(chunk)
        return b"".join(chunks).decode()


def write_stats(response: str) -> tuple[int, int]:
    reader = csv.DictReader(io.StringIO(response.removeprefix("# ")))
    backend_queue = None
    server = None
    for row in reader:
        if row.get("pxname") == "seaweed_s3_write" and row.get("svname") == "BACKEND":
            backend_queue = int(row["qcur"])
        if row.get("pxname") == "seaweed_s3_write" and row.get("svname") == "worker_s3_write":
            server = int(row["qcur"]), int(row["scur"])
    if server is not None:
        return max(server[0], backend_queue or 0), server[1]
    raise ValueError("HAProxy write server statistics missing")


class AdmissionController:
    def __init__(self, config, health, stopping) -> None:
        self.config = config
        self.health = health
        self.stopping = stopping
        admission = config.s3.admission
        self.budget = WriteBudget(admission.max_active_write_requests, admission.healthy_window_seconds)
        self.errors = 0
        self.path = config.appliance.runtime_dir / "admission.sock"

    def tick(self) -> None:
        snapshot = self.health.snapshot()
        now = time.monotonic()
        started = snapshot["probe_started_monotonic"]
        slow = self.config.s3.admission.slow_probe_seconds
        stale_after = self.config.appliance.probe_interval_seconds + self.config.appliance.probe_timeout_seconds + 2
        healthy = (
            snapshot["ready"]
            and snapshot["last_probe_at"] > 0
            and time.time() - snapshot["last_probe_at"] < stale_after
            and snapshot["last_probe_duration_seconds"] < slow
            and (not started or now - started < slow)
        )
        queue = active = 0
        error = ""
        previous = self.budget.limit
        try:
            queue, active = write_stats(runtime_command(self.path, "show stat"))
            limit = self.budget.update(now, healthy and queue == 0)
            reply = runtime_command(self.path, f"set maxconn server seaweed_s3_write/worker_s3_write {limit}")
            if reply.strip():
                raise OSError(f"HAProxy rejected write limit: {reply.strip()}")
        except (OSError, ValueError, KeyError) as exc:
            self.errors += 1
            self.budget.update(now, False)
            error = str(exc)
            # A stats error must not prevent an attempted reduction of an expanded limit.
            try:
                runtime_command(self.path, "set maxconn server seaweed_s3_write/worker_s3_write 1")
            except OSError:
                pass
            if self.errors == 1 or self.errors % 60 == 0:
                event("warning", "admission_control_failed", error=error)
        if previous != self.budget.limit:
            event("info", "admission_write_limit_changed", previous=previous, limit=self.budget.limit, queue=queue)
        self.health.set_admission({
            "write_limit": self.budget.limit, "queue": queue, "active": active,
            "backoffs_total": self.budget.backoffs, "control_errors_total": self.errors,
            "last_error": error, "applied": not error,
        })

    def start(self) -> None:
        def run() -> None:
            while not self.stopping():
                self.tick()
                time.sleep(1)
        threading.Thread(target=run, name="s3-admission", daemon=True).start()
