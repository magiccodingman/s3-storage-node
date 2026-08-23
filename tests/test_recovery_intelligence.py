from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from s3_storage_node.guardian import Guardian
from s3_storage_node.seaweed_health import SeaweedHealthError
from s3_storage_node.storage import StorageError


class FakeProcess:
    def __init__(self, running: bool = True) -> None:
        self.name = "volume"
        self._running = running

    def running(self) -> bool:
        return self._running

    def exit_code(self) -> int:
        return 7


def make_guardian() -> Guardian:
    appliance = SimpleNamespace(
        recovery_initial_seconds=1,
        recovery_max_seconds=4,
        recovery_stability_seconds=0,
        recovery_probe_interval_seconds=0,
        recovery_successes_required=3,
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
    return Guardian(SimpleNamespace(appliance=appliance, seaweed=seaweed, active_targets=()), "/config.toml")


def test_online_probe_failure_enters_suspect_before_recovery() -> None:
    guardian = make_guardian()
    guardian.haproxy = FakeProcess()
    guardian.processes = [FakeProcess()]
    guardian._probe_targets = Mock(side_effect=StorageError("share reconnecting"))

    with pytest.raises(StorageError, match="reconnecting"):
        guardian._online_loop()

    snapshot = guardian.health.snapshot()
    assert snapshot["state"] == "SUSPECT"
    assert snapshot["ready"] is False


def test_recovery_requires_multiple_successful_full_probes() -> None:
    guardian = make_guardian()
    events: list[str] = []
    guardian._probe_targets = Mock(side_effect=lambda full: events.append(f"probe:{full}"))
    guardian._run_s3_canary = Mock(side_effect=lambda: events.append("canary"))
    guardian._interruptible_sleep = Mock()

    guardian._stabilize_recovery()

    assert events == ["probe:True", "canary"] * 3
    assert guardian.health.snapshot()["state"] == "VERIFYING_RECOVERY"


def test_probe_health_tracks_success_and_failure() -> None:
    guardian = make_guardian()
    guardian.config.active_targets = (SimpleNamespace(name="data"),)
    guardian._run_probe = Mock(return_value={"free_bytes": 10})
    guardian._probe_targets(full=False)
    assert guardian.health.snapshot()["consecutive_probe_successes"] == 1

    guardian._run_probe = Mock(side_effect=StorageError("dead mount"))
    with pytest.raises(StorageError):
        guardian._probe_targets(full=False)
    snapshot = guardian.health.snapshot()
    assert snapshot["consecutive_probe_successes"] == 0
    assert snapshot["last_failure"] == "dead mount"


def test_unexpected_upstream_readonly_volume_fails_health(monkeypatch: pytest.MonkeyPatch) -> None:
    guardian = make_guardian()
    guardian.config.seaweed.volume_health_enabled = True
    guardian.config.seaweed.expected_readonly_volume_ids = ()
    monkeypatch.setattr(
        "s3_storage_node.guardian.inspect_volume_status",
        lambda *_args, **_kwargs: {
            "checked": True,
            "total": 4,
            "readonly": 3,
            "writable": 1,
            "unexpected_readonly": 3,
            "unexpected_readonly_volume_ids": [21, 22, 23],
            "collections": {"data": {"total": 4, "readonly": 3, "writable": 1}},
        },
    )

    with pytest.raises(SeaweedHealthError, match="21,22,23"):
        guardian._check_seaweed_volumes()

    assert guardian.health.snapshot()["seaweed_volumes"]["unexpected_readonly"] == 3


def volume_status(readonly_ids: list[int]) -> dict[str, object]:
    return {
        "checked": True,
        "total": 3,
        "readonly": len(readonly_ids),
        "writable": 3 - len(readonly_ids),
        "expected_readonly": 0,
        "unexpected_readonly": len(readonly_ids),
        "readonly_volume_ids": readonly_ids,
        "unexpected_readonly_volume_ids": readonly_ids,
        "volume_details": [
            {
                "id": volume_id,
                "collection": "data",
                "readonly": volume_id in readonly_ids,
                "expected_readonly": False,
            }
            for volume_id in (1, 2, 3)
        ],
        "collections": {},
    }


def test_startup_wait_ignores_transient_global_readonly_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardian = make_guardian()
    guardian.config.seaweed.volume_health_enabled = True
    guardian.config.seaweed.expected_readonly_volume_ids = ()
    guardian.config.appliance.recovery_successes_required = 2
    transient = volume_status([1, 2, 3])
    settled = volume_status([3])
    inspect = Mock(side_effect=[transient, settled, settled])
    monkeypatch.setattr("s3_storage_node.guardian.inspect_volume_status", inspect)
    guardian._interruptible_sleep = Mock()

    with pytest.raises(SeaweedHealthError, match="3") as exc_info:
        guardian._wait_for_stable_seaweed_volumes()

    assert exc_info.value.status["unexpected_readonly_volume_ids"] == [3]
    assert inspect.call_count == 3


def test_startup_wait_accepts_only_repeated_stable_volume_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardian = make_guardian()
    guardian.config.seaweed.volume_health_enabled = True
    guardian.config.seaweed.expected_readonly_volume_ids = ()
    guardian.config.appliance.recovery_successes_required = 2
    transient = volume_status([1, 2, 3])
    settled = volume_status([])
    inspect = Mock(side_effect=[transient, settled, settled])
    monkeypatch.setattr("s3_storage_node.guardian.inspect_volume_status", inspect)
    guardian._interruptible_sleep = Mock()

    assert guardian._wait_for_stable_seaweed_volumes() == settled
    assert inspect.call_count == 3
