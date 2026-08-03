from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from s3_storage_node.writer_lease import (
    LeaseRecord,
    WriterLeaseConfig,
    WriterLeaseController,
    WriterLeaseError,
    WriterLeaseLost,
    load_writer_lease,
)


def fake_config():
    appliance = SimpleNamespace(name="node-a", worker_fencing_mode="namespace")
    return SimpleNamespace(appliance=appliance, data_target=SimpleNamespace(sentinel_id="dataset-a"))


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_local_is_default(tmp_path: Path) -> None:
    cfg = load_writer_lease(str(write_config(tmp_path, "")), fake_config())
    assert cfg.backend == "local"
    assert cfg.lease_name == "dataset-a"


def test_postgres_requires_namespace_fencing(tmp_path: Path) -> None:
    config = fake_config()
    config.appliance.worker_fencing_mode = "disabled"
    path = write_config(tmp_path, """
[writer_lease]
backend = "postgres"
lease_name = "dataset-a"
postgres_dsn_file = "/run/secrets/dsn"
""")
    with pytest.raises(WriterLeaseError, match="requires appliance.worker_fencing_mode=namespace"):
        load_writer_lease(str(path), config)


def test_timing_window_is_validated(tmp_path: Path) -> None:
    path = write_config(tmp_path, """
[writer_lease]
backend = "postgres"
lease_name = "dataset-a"
postgres_dsn_file = "/run/secrets/dsn"
ttl_seconds = 10
renew_interval_seconds = 8
fence_margin_seconds = 2
""")
    with pytest.raises(WriterLeaseError, match="must be less than ttl_seconds"):
        load_writer_lease(str(path), fake_config())


class FakeBackend:
    def __init__(self) -> None:
        self.token = 0
        self.renew_error: Exception | None = None
        self.renew_none = False
        self.blocked = False
        self.released = False

    def initialize(self) -> None:
        pass

    def acquire(self, owner_id: str):
        self.token += 1
        return LeaseRecord("dataset", owner_id, self.token, time.time() + 5)

    def renew(self, owner_id: str, fencing_token: int):
        if self.renew_error:
            raise self.renew_error
        if self.renew_none:
            return None
        return LeaseRecord("dataset", owner_id, fencing_token, time.time() + 5)

    def release(self, owner_id: str, fencing_token: int) -> bool:
        self.released = True
        return True

    def status(self):
        return None

    def block_takeover(self, owner_id: str, fencing_token: int, reason: str) -> bool:
        self.blocked = True
        return True

    def unblock(self, expected_token: int) -> bool:
        self.blocked = False
        return True


def lease_config() -> WriterLeaseConfig:
    return WriterLeaseConfig(
        backend="postgres",
        lease_name="dataset",
        node_id="node-a",
        ttl_seconds=5,
        renew_interval_seconds=1,
        retry_interval_seconds=1,
        fence_margin_seconds=1,
        connect_timeout_seconds=1,
        postgres_dsn_file="/tmp/dsn",
    )


def test_controller_acquires_and_releases() -> None:
    backend = FakeBackend()
    controller = WriterLeaseController(lease_config(), backend=backend)
    assert controller.acquire() == 1
    assert controller.healthy
    controller.close()
    assert backend.released


def test_rejected_renewal_marks_lost() -> None:
    backend = FakeBackend()
    backend.renew_none = True
    lost: list[str] = []
    controller = WriterLeaseController(lease_config(), backend=backend, on_lost=lost.append)
    controller.acquire()
    deadline = time.time() + 3
    while not lost and time.time() < deadline:
        time.sleep(0.05)
    assert lost
    with pytest.raises(WriterLeaseLost):
        controller.assert_usable()
    controller.close(release=False)


def test_fencing_failure_blocks_takeover() -> None:
    backend = FakeBackend()
    controller = WriterLeaseController(lease_config(), backend=backend)
    controller.acquire()
    assert controller.block_takeover("veth survived")
    assert backend.blocked
    assert controller.snapshot()["takeover_blocked"] is True
    controller.close()
    assert not backend.released
