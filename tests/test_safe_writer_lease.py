from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from s3_storage_node.safe_writer_lease import (
    SafeWriterLeaseController,
    WriterLeaseOperationTimeout,
    load_writer_lease,
)
from s3_storage_node.writer_lease import LeaseRecord, WriterLeaseConfig, WriterLeaseError, WriterLeaseLost


def config_object():
    return SimpleNamespace(
        appliance=SimpleNamespace(name="node-a", worker_fencing_mode="namespace"),
        data_target=SimpleNamespace(sentinel_id="dataset-a"),
    )


def test_safe_timing_budget_is_validated(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[writer_lease]
backend = "postgres"
lease_name = "dataset-a"
postgres_dsn_file = "/run/secrets/dsn"
ttl_seconds = 10
renew_interval_seconds = 5
connect_timeout_seconds = 3
fence_margin_seconds = 2
""",
        encoding="utf-8",
    )
    with pytest.raises(WriterLeaseError, match="connect_timeout_seconds"):
        load_writer_lease(str(path), config_object())


class SkewedBackend:
    def __init__(self, *, renew_sleep: float = 0.0) -> None:
        self.renew_sleep = renew_sleep
        self.token = 1

    def initialize(self) -> None:
        pass

    def acquire(self, owner_id: str):
        # Deliberately impossible wall-clock expiry. The safe controller must
        # use measured local elapsed time and configured TTL instead.
        return LeaseRecord("dataset", owner_id, self.token, 1.0)

    def renew(self, owner_id: str, fencing_token: int):
        time.sleep(self.renew_sleep)
        return LeaseRecord("dataset", owner_id, fencing_token, 1.0)

    def release(self, owner_id: str, fencing_token: int) -> bool:
        return True

    def status(self):
        return None

    def block_takeover(self, owner_id: str, fencing_token: int, reason: str) -> bool:
        return True

    def unblock(self, expected_token: int) -> bool:
        return True


def lease_config(*, ttl: int = 5, renew: int = 1, margin: int = 1, timeout: int = 1):
    return WriterLeaseConfig(
        backend="postgres",
        lease_name="dataset",
        node_id="node-a",
        ttl_seconds=ttl,
        renew_interval_seconds=renew,
        retry_interval_seconds=1,
        fence_margin_seconds=margin,
        takeover_delay_seconds=0,
        connect_timeout_seconds=timeout,
        postgres_dsn_file="/tmp/dsn",
    )


def test_acquisition_deadline_ignores_database_wall_clock() -> None:
    controller = SafeWriterLeaseController(lease_config(), backend=SkewedBackend())
    controller.acquire()
    snapshot = controller.snapshot()
    assert 3.5 < snapshot["ttl_remaining_seconds"] <= 5.0
    controller.close()


def test_renewal_timeout_marks_lease_lost_without_waiting_for_late_result() -> None:
    lost: list[str] = []
    controller = SafeWriterLeaseController(
        lease_config(ttl=6, renew=1, margin=1, timeout=1),
        backend=SkewedBackend(renew_sleep=3),
        on_lost=lost.append,
    )
    controller.acquire()
    deadline = time.monotonic() + 3
    while not lost and time.monotonic() < deadline:
        time.sleep(0.05)
    assert lost
    assert "exceeded" in lost[0]
    with pytest.raises(WriterLeaseLost):
        controller.assert_usable()
    with pytest.raises(WriterLeaseOperationTimeout, match="still running"):
        controller.acquire()
    controller.close(release=False)
