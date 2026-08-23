from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Mapping

from .logging import event


class ProcessError(RuntimeError):
    pass


@dataclass
class ManagedProcess:
    name: str
    command: list[str]
    uid: int | None = None
    gid: int | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        env = os.environ.copy()
        env.update(self.environment)
        event("info", "process_starting", process=self.name, command=self.command)
        self.process = subprocess.Popen(
            self.command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            start_new_session=True,
            cwd=self.cwd,
            user=self.uid,
            group=self.gid,
            extra_groups=[] if self.uid is not None and self.gid is not None else None,
        )

    def stop(self, grace_seconds: int) -> bool:
        return self.stop_until(time.monotonic() + grace_seconds)

    def stop_until(self, deadline: float) -> bool:
        """Stop within a caller-owned global deadline."""

        if not self.process or self.process.poll() is not None:
            return True
        event("info", "process_stopping", process=self.name, pid=self.process.pid)
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        try:
            self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            event("warning", "process_killing", process=self.name, pid=self.process.pid)
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                event("error", "process_stuck", process=self.name, pid=self.process.pid)
                return False
        return True

    def running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def exit_code(self) -> int | None:
        return None if not self.process else self.process.poll()


def wait_for_tcp(host: str, port: int, timeout_seconds: int, process: ManagedProcess | None = None) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process and not process.running():
            raise ProcessError(f"{process.name} exited with code {process.exit_code()}")
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise ProcessError(f"timed out waiting for {host}:{port}")
