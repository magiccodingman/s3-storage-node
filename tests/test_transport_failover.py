from pathlib import Path

import pytest

from s3_storage_node.config_helpers import ConfigError
from s3_storage_node.config_types import ApplianceConfig, Config, IndexConfig, MetadataConfig, S3Config, SeaweedConfig, TargetConfig
from s3_storage_node.transport_failover import TransportSelector, load_exclusive_failover, startup_verification_fingerprint


def make_config(tmp_path: Path, mode: str = "namespace") -> Config:
    target = TargetConfig(
        name="data", type="cifs", mountpoint=tmp_path / "run/mounts/data", subdirectory="seaweedfs",
        sentinel_id="dataset", source="//box/share", credentials_file=str(tmp_path / "cifs"),
        io_failure_policy="soft",
    )
    metadata = TargetConfig(name="metadata", type="path", mountpoint=tmp_path / "metadata", subdirectory="", sentinel_id="m")
    index = TargetConfig(name="index", type="path", mountpoint=tmp_path / "index", subdirectory="", sentinel_id="i")
    return Config(
        appliance=ApplianceConfig(state_dir=tmp_path / "state", runtime_dir=tmp_path / "run", worker_fencing_mode=mode),
        targets={"data": target, "metadata": metadata, "index": index},
        metadata=MetadataConfig(), index=IndexConfig(), seaweed=SeaweedConfig(), s3=S3Config(),
    )


def write_config(path: Path, *, auth: str = "key", verify: bool = True) -> None:
    auth_lines = (
        'auth_mode = "key"\nidentity_file = "/run/secrets/key"'
        if auth == "key" else 'auth_mode = "password"\ncredentials_file = "/run/secrets/cifs"'
    )
    path.write_text(f'''
[storage.data.failover]
enabled = true
primary_name = "cifs-primary"
primary_priority = 10
failback_policy = "manual"
failure_cooldown_seconds = 60
verify_all_transports_on_startup = {str(verify).lower()}

[[storage.data.failover.transports]]
name = "sshfs-secondary"
type = "sshfs"
priority = 20
source = "user@host:/remote"
{auth_lines}
known_hosts_file = "/run/secrets/known_hosts"
port = 23
mount_options = ["no_readahead"]
''', encoding="utf-8")


def test_parses_and_resolves_key_sshfs_transport(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    config = make_config(tmp_path)
    failover = load_exclusive_failover(path, config)
    assert failover is not None
    assert failover.ordered_names == ("cifs-primary", "sshfs-secondary")
    assert failover.verify_all_transports_on_startup is True
    target = failover.resolve(config.data_target, "sshfs-secondary")
    assert target.ssh_auth_mode == "key"
    assert "PreferredAuthentications=publickey" in target.mount_options
    assert "StrictHostKeyChecking=yes" in target.mount_options


def test_parses_password_auth_and_reuses_cifs_credentials_by_default(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, auth="password")
    path.write_text(path.read_text().replace('credentials_file = "/run/secrets/cifs"\n', "", 1))
    config = make_config(tmp_path)
    failover = load_exclusive_failover(path, config)
    assert failover is not None
    target = failover.resolve(config.data_target, "sshfs-secondary")
    assert target.ssh_auth_mode == "password"
    assert target.ssh_credentials_file == config.data_target.credentials_file
    assert "password_stdin" in target.mount_options
    assert "PreferredAuthentications=password" in target.mount_options
    assert not any(option.startswith("IdentityFile=") for option in target.mount_options)


def test_legacy_identity_file_infers_key_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    path.write_text(path.read_text().replace('auth_mode = "key"\n', ""))
    failover = load_exclusive_failover(path, make_config(tmp_path))
    assert failover is not None
    assert failover.transports[0].auth_mode == "key"


def test_credentials_file_without_mode_infers_password(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, auth="password")
    path.write_text(path.read_text().replace('auth_mode = "password"\n', ""))
    failover = load_exclusive_failover(path, make_config(tmp_path))
    assert failover is not None
    assert failover.transports[0].auth_mode == "password"


def test_password_mode_rejects_identity_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, auth="password")
    path.write_text(path.read_text().replace(
        'credentials_file = "/run/secrets/cifs"',
        'credentials_file = "/run/secrets/cifs"\nidentity_file = "/run/secrets/key"',
    ))
    with pytest.raises(ConfigError, match="identity_file is only valid with auth_mode=key"):
        load_exclusive_failover(path, make_config(tmp_path))


def test_key_mode_rejects_credentials_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    path.write_text(path.read_text().replace(
        'identity_file = "/run/secrets/key"',
        'identity_file = "/run/secrets/key"\ncredentials_file = "/run/secrets/cifs"',
    ))
    with pytest.raises(ConfigError, match="credentials_file is only valid with auth_mode=password"):
        load_exclusive_failover(path, make_config(tmp_path))


def test_startup_verification_can_be_disabled(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, verify=False)
    failover = load_exclusive_failover(path, make_config(tmp_path))
    assert failover is not None
    assert failover.verify_all_transports_on_startup is False


def test_startup_verification_fingerprint_changes_with_credentials(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, auth="password")
    config = make_config(tmp_path)
    credentials = Path(config.data_target.credentials_file)
    credentials.write_text("username=user\npassword=first\n")
    failover = load_exclusive_failover(path, config)
    assert failover is not None
    first = startup_verification_fingerprint(config, failover)
    credentials.write_text("username=user\npassword=second\n")
    assert first != startup_verification_fingerprint(config, failover)


def test_selector_remembers_successful_startup_verification(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    failover = load_exclusive_failover(path, make_config(tmp_path))
    assert failover is not None
    selector = TransportSelector(tmp_path / "state/guardian", failover)
    assert selector.startup_verification_required("abc") is True
    selector.record_startup_verification("abc", failover.ordered_names)
    assert selector.startup_verification_required("abc") is False
    assert selector.startup_verification_required("def") is True
    status = selector.status()
    assert status["startup_verified_transports"] == list(failover.ordered_names)
    assert "startup_verification_fingerprint" not in status


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
    path.write_text(path.read_text().replace('mount_options = ["no_readahead"]', 'mount_options = ["ProxyCommand=oops"]'))
    with pytest.raises(ConfigError, match="not in the guarded SSHFS tuning allowlist"):
        load_exclusive_failover(path, make_config(tmp_path))
