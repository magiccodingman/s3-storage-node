from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from s3_storage_node.health import HealthState
from s3_storage_node.lease_guardian import Guardian
from s3_storage_node.transport_guardian import Guardian as TransportGuardian


def bare_guardian() -> Guardian:
    guardian = Guardian.__new__(Guardian)
    guardian.writer_lease_config = SimpleNamespace(
        backend="postgres", lease_name="dataset-a", node_id="node-a"
    )
    guardian.writer_epoch = Mock(unsafe=True)
    guardian.writer_epoch.fencing_token = 41
    guardian.writer_epoch.snapshot.return_value = {
        "backend": "postgres",
        "scope": "distributed",
        "lease_name": "dataset-a",
        "owner": "node-a:session",
        "node_id": "node-a",
        "held": True,
        "healthy": True,
        "at_risk": False,
        "lost": False,
        "takeover_blocked": False,
        "fencing_token": 41,
        "lease_until_epoch": 123.0,
        "ttl_remaining_seconds": 10.0,
        "renewals_total": 2,
        "renewal_failures_total": 0,
        "last_error": "",
    }
    guardian.health = HealthState()
    guardian.generation = None
    guardian.processes = []
    guardian.fatal_fence_failure = False
    guardian.stopping = False
    guardian._writer_epoch_lost_event = threading.Event()
    guardian._writer_epoch_callback_lock = threading.Lock()
    return guardian


def test_begin_generation_acquires_writer_epoch_first(monkeypatch) -> None:
    guardian = bare_guardian()
    events: list[str] = []
    guardian.writer_epoch.acquire.side_effect = lambda: events.append("acquire") or 41
    guardian.writer_epoch.assert_usable.side_effect = lambda: events.append("assert")
    monkeypatch.setattr(TransportGuardian, "_begin_generation", lambda self: events.append("generation"))
    guardian._publish_generation = Mock()

    Guardian._begin_generation(guardian)

    assert events[:3] == ["acquire", "assert", "generation"]
    assert os.environ["S3_STORAGE_NODE_FENCING_TOKEN"] == "41"


def test_lease_loss_withdraws_readiness_and_fences() -> None:
    guardian = bare_guardian()
    guardian.health.set("ONLINE", True, "healthy")
    guardian._fence_generation = Mock(return_value=True)

    guardian._writer_epoch_lost("renewal deadline exceeded")

    snapshot = guardian.health.snapshot()
    assert snapshot["state"] == "WRITER_LEASE_LOST"
    assert snapshot["ready"] is False
    assert guardian._writer_epoch_lost_event.is_set()
    guardian._fence_generation.assert_called_once()


def test_fence_failure_blocks_distributed_takeover(monkeypatch) -> None:
    guardian = bare_guardian()
    guardian.writer_epoch.block_takeover.return_value = True
    monkeypatch.setattr(TransportGuardian, "_fence_generation", lambda self, reason: False)

    assert Guardian._fence_generation(guardian, "veth survived") is False
    guardian.writer_epoch.block_takeover.assert_called_once_with("veth survived")


def test_writer_epoch_is_exposed_in_health() -> None:
    guardian = bare_guardian()
    guardian._publish_writer_epoch(guardian.writer_epoch.snapshot())
    snapshot = guardian.health.snapshot()
    assert snapshot["writer"]["fencing_token"] == 41
    assert snapshot["writer"]["scope"] == "distributed"
    assert snapshot["generation"]["writer_fencing_token"] == 41
