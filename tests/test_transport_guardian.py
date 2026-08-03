from pathlib import Path
from unittest.mock import Mock

import pytest

from s3_storage_node.config_types import (
    ApplianceConfig,
    Config,
    IndexConfig,
    MetadataConfig,
    S3Config,
    SeaweedConfig,
    TargetConfig,
)
from s3_storage_node.transport_guardian import Guardian, TransportSwitchRequested


def make_config(tmp_path: Path) -> Config:
    targets = {
        "data": TargetConfig(
            name="data",
            type="cifs",
            mountpoint=tmp_path / "run/mounts/data",
            subdirectory="seaweedfs",
            sentinel_id="dataset",
            source="//box/share",
            credentials_file="/run/secrets/cifs",
        ),
        "metadata": TargetConfig(
            name="metadata", type="path", mountpoint=tmp_path / "metadata", subdirectory="", sentinel_id="metadata"
        ),
        "index": TargetConfig(
            name="index", type="path", mountpoint=tmp_path / "index", subdirectory="", sentinel_id="index"
        ),
    }
    return Config(
        appliance=ApplianceConfig(
            name="node-a",
            state_dir=tmp_path / "state",
            runtime_dir=tmp_path / "run",
            worker_fencing_mode="namespace",
        ),
        targets=targets,
        metadata=MetadataConfig(),
        index=IndexConfig(),
        seaweed=SeaweedConfig(),
        s3=S3Config(),
    )


def write_failover(path: Path) -> None:
    path.write_text(
        '''
[storage.data.failover]
enabled = true
primary_name = "cifs-primary"
failure_cooldown_seconds = 60

[[storage.data.failover.transports]]
name = "sshfs-secondary"
source = "user@host:/remote"
identity_file = "/run/secrets/key"
known_hosts_file = "/run/secrets/known_hosts"
''',
        encoding="utf-8",
    )


def fake_generation(number: int) -> Mock:
    generation = Mock()
    generation.generation = number
    generation.token = f"token-{number}"
    generation.mode = "namespace"
    generation.namespace_pid = 1000 + number
    generation.worker_ip = "169.254.254.2"
    generation.fenced = False
    generation.fence_reason = ""

    def fence(reason: str) -> None:
        generation.fenced = True
        generation.fence_reason = reason

    generation.fence.side_effect = fence
    return generation


def test_transport_failure_rotates_generation_to_secondary(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    first = fake_generation(1)
    second = fake_generation(2)
    guardian.generation_factory.create = Mock(side_effect=[first, second])

    guardian._begin_generation()
    assert guardian.active_transport == "cifs-primary"
    guardian._transport_failure = True
    assert guardian._fence_generation("storage probe failed") is True

    guardian._begin_generation()
    assert guardian.active_transport == "sshfs-secondary"
    assert first.retire.called
    transport = guardian.health.snapshot()["storage"]["transport:data"]
    assert transport["using_primary"] == 0


def test_non_storage_failure_keeps_current_transport(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.generation_factory.create = Mock(side_effect=[fake_generation(1), fake_generation(2)])

    guardian._begin_generation()
    assert guardian._fence_generation("HAProxy exited") is True
    guardian._begin_generation()
    assert guardian.active_transport == "cifs-primary"


def test_manual_switch_is_fenced_but_not_recorded_as_failure(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.generation_factory.create = Mock(side_effect=[fake_generation(1), fake_generation(2)])

    guardian._begin_generation()
    guardian.transport_selector.request("sshfs-secondary")
    guardian._online_transport_watch = True
    with pytest.raises(TransportSwitchRequested, match="operator requested"):
        guardian._interruptible_sleep(1)
    assert guardian._controlled_transport_switch is True
    assert guardian._fence_generation("operator requested transport switch") is True

    guardian._begin_generation()
    assert guardian.active_transport == "sshfs-secondary"
    status = guardian.transport_selector.status()
    assert "cifs-primary" not in status["failed"]
