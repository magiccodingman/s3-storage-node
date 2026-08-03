from pathlib import Path

import pytest

from s3_storage_node.config_helpers import ConfigError
from s3_storage_node.config_types import (
    ApplianceConfig,
    Config,
    IndexConfig,
    MetadataConfig,
    S3Config,
    SeaweedConfig,
    TargetConfig,
)
from s3_storage_node.transport_failover import TransportSelector, load_exclusive_failover


def make_config(tmp_path: Path, mode: str = "namespace") -> Config:
    target = TargetConfig(
        name="data",
        type="cifs",
        mountpoint=tmp_path / "run/mounts/data",
        subdirectory="seaweedfs",
        sentinel_id="dataset",
        source="//box/share",
        credentials_file="/run/secrets/cifs",
        io_failure_policy="soft",
    )
    metadata = TargetConfig(
        name="metadata", type="path", mountpoint=tmp_path / "metadata", subdirectory="", sentinel_id="m"
    )
    index = TargetConfig(name="index", type="path", mountpoint=tmp_path / "index", subdirectory="", sentinel_id="i")
    return Config(
        appliance=ApplianceConfig(
            state_dir=tmp_path / "state", runtime_dir=tmp_path / "run", worker_fencing_mode=mode
        ),
        targets={"data": target, "metadata": metadata, "index": index},
        metadata=MetadataConfig(),
        index=IndexConfig(),
        seaweed=SeaweedConfig(),
        s3=S3Config(),
    )


def write_config(path: Path) -> None:
    path.write_text(
        '''
[storage.data.failover]
enabled = true
primary_name = "cifs-primary"
primary_priority = 10
failback_policy = "manual"
failure_cooldown_seconds = 60

[[storage.data.failover.transports]]
name = "sshfs-secondary"
type = "sshfs"
priority = 20
source = "user@host:/remote"
identity_file = "/run/secrets/key"
known_hosts_file = "/run/secrets/known_hosts"
port = 23
mount_options = ["no_readahead"]
''',
        encoding="utf-8",
    )


def test_parses_and_resolves_sshfs_transport(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    config = make_config(tmp_path)
    failover = load_exclusive_failover(path, config)
    assert failover is not None
    assert failover.ordered_names == ("cifs-primary", "sshfs-secondary")
    target = failover.resolve(config.data_target, "sshfs-secondary")
    assert target.type == "sshfs"
    assert target.transport_name == "sshfs-secondary"
    assert target.ssh_port == 23
    assert "sshfs_sync" in target.mount_options
    assert "StrictHostKeyChecking=yes" in target.mount_options
    assert "uid=10001" in target.mount_options


def test_failover_requires_namespace_fencing(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    with pytest.raises(ConfigError, match="worker_fencing_mode=namespace"):
        load_exclusive_failover(path, make_config(tmp_path, mode="disabled"))


def test_guardian_owned_sshfs_options_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    path.write_text(path.read_text().replace('mount_options = ["no_readahead"]', 'mount_options = ["reconnect"]'))
    with pytest.raises(ConfigError, match="not in the guarded SSHFS tuning allowlist"):
        load_exclusive_failover(path, make_config(tmp_path))


def test_selector_rotates_then_sticks_to_healthy_fallback(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    failover = load_exclusive_failover(path, make_config(tmp_path))
    assert failover is not None
    selector = TransportSelector(tmp_path / "state/guardian", failover)
    assert selector.select(now=100) == "cifs-primary"
    selector.record_failure("cifs-primary", "down", now=100)
    assert selector.select(now=101) == "sshfs-secondary"
    selector.record_success("sshfs-secondary", now=102)
    assert selector.select(now=1000) == "sshfs-secondary"


def test_manual_request_switches_without_marking_current_failed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    failover = load_exclusive_failover(path, make_config(tmp_path))
    assert failover is not None
    selector = TransportSelector(tmp_path / "state/guardian", failover)
    selector.record_success("sshfs-secondary", now=100)
    selector.request("cifs-primary")
    assert selector.pending_request() == "cifs-primary"
    assert selector.select(now=101) == "cifs-primary"
    assert selector.pending_request() == ""


def test_requesting_current_transport_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    failover = load_exclusive_failover(path, make_config(tmp_path))
    assert failover is not None
    selector = TransportSelector(tmp_path / "state/guardian", failover)
    selector.record_success("cifs-primary", now=100)
    selector.request("cifs-primary")
    assert selector.pending_request() == ""


def test_selector_enforces_cooldown_when_every_transport_failed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    failover = load_exclusive_failover(path, make_config(tmp_path))
    assert failover is not None
    selector = TransportSelector(tmp_path / "state/guardian", failover)
    selector.record_failure("cifs-primary", "down", now=100)
    selector.record_failure("sshfs-secondary", "down", now=110)
    with pytest.raises(Exception, match="all configured transports are cooling down"):
        selector.select(now=111)
    assert selector.select(now=161) == "cifs-primary"


def test_transport_names_cannot_escape_runtime_directory(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    path.write_text(path.read_text().replace('name = "sshfs-secondary"', 'name = "../escape"'))
    with pytest.raises(ConfigError, match="must use 1-64"):
        load_exclusive_failover(path, make_config(tmp_path))


def test_sshfs_options_use_a_safe_allowlist(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    path.write_text(
        path.read_text().replace('mount_options = ["no_readahead"]', 'mount_options = ["ProxyCommand=oops"]')
    )
    with pytest.raises(ConfigError, match="not in the guarded SSHFS tuning allowlist"):
        load_exclusive_failover(path, make_config(tmp_path))
