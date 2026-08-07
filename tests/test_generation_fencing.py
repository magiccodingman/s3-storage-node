from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from s3_storage_node.generation import GenerationError, GenerationFactory, LocalWriterLease, WorkerGeneration
from s3_storage_node.generation_guardian import Guardian
from s3_storage_node.render import render_haproxy


def test_local_writer_lease_is_exclusive(tmp_path: Path) -> None:
    first = LocalWriterLease(tmp_path, "node-a")
    second = LocalWriterLease(tmp_path, "node-b")
    first.acquire()
    try:
        with pytest.raises(GenerationError, match="already held"):
            second.acquire()
        payload = json.loads((tmp_path / "writer.lock").read_text(encoding="utf-8"))
        assert payload["node"] == "node-a"
    finally:
        first.release()
    second.acquire()
    second.release()


def test_generation_counter_is_monotonic(tmp_path: Path) -> None:
    factory = GenerationFactory(tmp_path / "state", tmp_path / "run")
    one = factory.create(
        mode="disabled",
        host_address="169.254.254.1/30",
        worker_address="169.254.254.2/30",
        gateway="169.254.254.1",
    )
    two = factory.create(
        mode="disabled",
        host_address="169.254.254.1/30",
        worker_address="169.254.254.2/30",
        gateway="169.254.254.1",
    )
    assert (one.generation, two.generation) == (1, 2)
    assert one.token != two.token


def test_namespace_command_enters_mount_and_network_namespaces(tmp_path: Path) -> None:
    keeper = Mock()
    keeper.pid = 4321
    keeper.poll.return_value = None
    generation = WorkerGeneration(
        generation=7,
        token="token",
        mode="namespace",
        state_dir=tmp_path,
        runtime_dir=tmp_path / "run",
        host_address="169.254.254.1/30",
        worker_address="169.254.254.2/30",
        gateway="169.254.254.1",
        keeper=keeper,
    )
    assert generation.enter_command(["weed", "volume"], 10001, 10001) == [
        "nsenter", "--target", "4321", "--mount", "--net", "--",
        "setpriv", "--reuid=10001", "--regid=10001", "--clear-groups", "--",
        "weed", "volume",
    ]


def make_guardian(tmp_path: Path) -> Guardian:
    appliance = SimpleNamespace(
        name="node-a",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        worker_fencing_mode="namespace",
        worker_host_address="169.254.254.1/30",
        worker_address="169.254.254.2/30",
        worker_gateway="169.254.254.1",
        uid=10001,
        gid=10001,
        shutdown_grace_seconds=1,
    )
    config = SimpleNamespace(appliance=appliance, active_targets=())
    return Guardian(config, "/config.toml")


def test_guardian_fences_generation_before_stopping_processes(tmp_path: Path) -> None:
    guardian = make_guardian(tmp_path)
    events: list[str] = []
    generation = Mock()
    generation.generation = 4
    generation.token = "token"
    generation.mode = "namespace"
    generation.namespace_pid = 44
    generation.worker_ip = "169.254.254.2"
    generation.fenced = False
    generation.fence_reason = ""

    def fence(reason: str) -> None:
        events.append(f"fence:{reason}")
        generation.fenced = True
        generation.fence_reason = reason

    generation.fence.side_effect = fence
    guardian.generation = generation
    guardian.processes = []
    guardian._stop_seaweed = Mock(side_effect=lambda: events.append("stop"))

    guardian._fence_generation("storage lost")
    guardian._stop_seaweed()

    assert events == ["fence:storage lost", "stop"]
    assert guardian.health.snapshot()["generation"]["fenced"] is True


def test_lingering_process_still_blocks_replacement_generation(tmp_path: Path) -> None:
    guardian = make_guardian(tmp_path)
    process = Mock()
    process.name = "volume"
    process.running.return_value = True
    guardian.lingering_processes = [process]

    with pytest.raises(Exception, match="still blocked: volume"):
        guardian._begin_generation()


def test_haproxy_routes_to_worker_namespace_but_checks_root_health(tmp_path: Path) -> None:
    config = SimpleNamespace(
        appliance=SimpleNamespace(runtime_dir=tmp_path, health_host="0.0.0.0", health_port=9090),
        s3=SimpleNamespace(
            host="0.0.0.0",
            port=8333,
            tls_mode="off",
            tls_pem_file="",
            admission=SimpleNamespace(enabled=False),
        ),
        seaweed=SimpleNamespace(s3_internal_port=18333),
        worker_endpoint_host="169.254.254.2",
    )
    path = render_haproxy(config)
    content = path.read_text(encoding="utf-8")
    assert "server worker_s3 169.254.254.2:18333" in content
    assert "check addr 127.0.0.1 port 9090" in content


def test_haproxy_respects_explicit_root_health_address(tmp_path: Path) -> None:
    config = SimpleNamespace(
        appliance=SimpleNamespace(runtime_dir=tmp_path, health_host="127.0.0.2", health_port=9090),
        s3=SimpleNamespace(
            host="0.0.0.0",
            port=8333,
            tls_mode="off",
            tls_pem_file="",
            admission=SimpleNamespace(enabled=False),
        ),
        seaweed=SimpleNamespace(s3_internal_port=18333),
        worker_endpoint_host="169.254.254.2",
    )
    content = render_haproxy(config).read_text(encoding="utf-8")
    assert "check addr 127.0.0.2 port 9090" in content


def test_fence_failure_is_fatal_and_reported(tmp_path: Path) -> None:
    guardian = make_guardian(tmp_path)
    generation = Mock()
    generation.generation = 9
    generation.token = "token"
    generation.mode = "namespace"
    generation.namespace_pid = 99
    generation.worker_ip = "169.254.254.2"
    generation.fenced = False
    generation.fence_reason = ""
    generation.fence.side_effect = GenerationError("veth still exists")
    guardian.generation = generation

    assert guardian._fence_generation("storage failed") is False
    snapshot = guardian.health.snapshot()
    assert snapshot["state"] == "FENCE_FAILED"
    assert snapshot["ready"] is False
    assert guardian.fatal_fence_failure is True
