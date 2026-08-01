from __future__ import annotations

import io
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from s3_storage_node.guardian import Guardian
from s3_storage_node.processes import ProcessError
from s3_storage_node.storage import StorageError


def make_guardian() -> Guardian:
    appliance = SimpleNamespace(
        recovery_initial_seconds=1,
        recovery_max_seconds=4,
        startup_timeout_seconds=2,
        probe_timeout_seconds=2,
        shutdown_grace_seconds=1,
        probe_interval_seconds=1,
        full_probe_interval_seconds=60,
        s3_canary_enabled=True,
        state_dir=None,
        runtime_dir=None,
        uid=10001,
        gid=10001,
    )
    seaweed = SimpleNamespace(
        master_port=9333,
        volume_port=8080,
        filer_port=8888,
        s3_internal_port=8334,
    )
    config = SimpleNamespace(appliance=appliance, seaweed=seaweed, active_targets=())
    return Guardian(config, "/config.toml")


class FakeProcess:
    def __init__(self, name: str, events: list[str], *, stops: bool = True, running: bool = True) -> None:
        self.name = name
        self.events = events
        self._stops = stops
        self._running = running

    def start(self) -> None:
        self.events.append(f"start:{self.name}")

    def stop(self, _grace: int) -> bool:
        self.events.append(f"stop:{self.name}")
        self._running = not self._stops
        return self._stops

    def running(self) -> bool:
        return self._running

    def exit_code(self) -> int | None:
        return 7


def test_start_seaweed_starts_in_dependency_order_and_runs_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    guardian = make_guardian()
    events: list[str] = []
    guardian._build_processes = Mock(return_value=[FakeProcess(name, events) for name in ("master", "volume", "filer", "s3")])
    monkeypatch.setattr("s3_storage_node.guardian.wait_for_tcp", lambda _host, port, _timeout, process: events.append(f"ready:{process.name}:{port}"))
    guardian._run_s3_canary = Mock(side_effect=lambda: events.append("canary"))

    guardian._start_seaweed()

    assert events == [
        "start:master", "ready:master:9333",
        "start:volume", "ready:volume:8080",
        "start:filer", "ready:filer:8888",
        "start:s3", "ready:s3:8334",
        "canary",
    ]


def test_stop_seaweed_reverses_dependency_order() -> None:
    guardian = make_guardian()
    events: list[str] = []
    guardian.processes = [FakeProcess(name, events) for name in ("master", "volume", "filer", "s3")]

    guardian._stop_seaweed()

    assert events == ["stop:s3", "stop:filer", "stop:volume", "stop:master"]
    assert guardian.processes == []


def test_process_that_cannot_stop_blocks_future_storage_start() -> None:
    guardian = make_guardian()
    events: list[str] = []
    stuck = FakeProcess("volume", events, stops=False)
    guardian.processes = [stuck]

    guardian._stop_seaweed()

    with pytest.raises(ProcessError, match="still blocked: volume"):
        guardian._ensure_no_lingering_processes()


def test_unhealthy_haproxy_withdraws_online_state_immediately() -> None:
    guardian = make_guardian()
    guardian.haproxy = FakeProcess("haproxy", [], running=False)

    with pytest.raises(ProcessError, match="HAProxy exited"):
        guardian._online_loop()


def test_dead_seaweed_child_withdraws_online_state() -> None:
    guardian = make_guardian()
    guardian.haproxy = FakeProcess("haproxy", [], running=True)
    guardian.processes = [FakeProcess("volume", [], running=False)]

    with pytest.raises(ProcessError, match="volume exited"):
        guardian._online_loop()


def test_probe_results_are_published_to_health() -> None:
    guardian = make_guardian()
    guardian.config.active_targets = (SimpleNamespace(name="data"), SimpleNamespace(name="metadata"))
    guardian._run_probe = Mock(side_effect=[{"free_bytes": 10}, {"free_bytes": 20}])

    guardian._probe_targets(full=True)

    assert guardian.health.snapshot()["storage"] == {
        "data": {"free_bytes": 10},
        "metadata": {"free_bytes": 20},
    }
    assert guardian._run_probe.call_args_list[0].args == ("data", True)
    assert guardian._run_probe.call_args_list[1].args == ("metadata", True)


def test_run_failure_stops_seaweed_before_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    guardian = make_guardian()
    events: list[str] = []
    monkeypatch.setattr(guardian, "_install_signals", lambda: None)
    monkeypatch.setattr(guardian, "_prepare_directories", lambda: None)
    monkeypatch.setattr("s3_storage_node.guardian.start_server", lambda *_args: None)
    monkeypatch.setattr(guardian, "_ensure_haproxy", lambda: events.append("gate-ready"))
    monkeypatch.setattr(guardian, "_mount_and_enroll_targets", lambda: (_ for _ in ()).throw(StorageError("disk gone")))
    monkeypatch.setattr(guardian, "_stop_seaweed", lambda: events.append("stop-seaweed"))
    monkeypatch.setattr(guardian, "_repair_targets", lambda unmount_all=False: events.append(f"repair:{unmount_all}"))

    def finish_after_backoff(_seconds: int) -> None:
        events.append("backoff")
        guardian.stopping = True

    monkeypatch.setattr(guardian, "_interruptible_sleep", finish_after_backoff)

    assert guardian.run() == 0
    assert events.index("stop-seaweed") < events.index("repair:False")
    assert guardian.health.snapshot()["failures_total"] == 1
    assert guardian.health.snapshot()["recovery_attempts"] == 1


def test_timed_out_helper_is_killed_and_quarantined(monkeypatch: pytest.MonkeyPatch) -> None:
    guardian = make_guardian()
    killed: list[tuple[int, int]] = []

    class BlockedHelper:
        pid = 321
        returncode = None
        stdout = io.StringIO()
        stderr = io.StringIO()

        def communicate(self, timeout: int):
            raise subprocess.TimeoutExpired("helper", timeout)

        def poll(self):
            return None

    helper = BlockedHelper()
    monkeypatch.setattr("s3_storage_node.guardian.subprocess.Popen", lambda *_args, **_kwargs: helper)
    monkeypatch.setattr("s3_storage_node.guardian.os.killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(StorageError, match="storage probe timed out for data"):
        guardian._run_helper("probe", "data", timeout=1)

    assert killed
    assert guardian.helper_children == [helper]


def test_previous_blocked_helper_prevents_overlapping_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    guardian = make_guardian()
    blocked = Mock()
    blocked.poll.return_value = None
    guardian.helper_children = [blocked]

    with pytest.raises(StorageError, match="previous storage helper is still blocked"):
        guardian._run_helper("mount", "data", timeout=1)
