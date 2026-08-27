from pathlib import Path
from unittest.mock import Mock

import pytest

from s3_storage_node.config_types import ApplianceConfig, Config, IndexConfig, MetadataConfig, S3Config, SeaweedConfig, TargetConfig
from s3_storage_node.generation_guardian import Guardian as GenerationGuardian
from s3_storage_node.processes import ProcessError
from s3_storage_node.storage import StorageError
from s3_storage_node.transport_guardian import Guardian, TransportSwitchRequested


def make_config(tmp_path: Path) -> Config:
    credentials = tmp_path / "cifs"
    credentials.write_text("username=user\npassword=secret\n")
    targets = {
        "data": TargetConfig(
            name="data", type="cifs", mountpoint=tmp_path / "run/mounts/data", subdirectory="seaweedfs",
            sentinel_id="dataset", source="//box/share", credentials_file=str(credentials),
        ),
        "metadata": TargetConfig(name="metadata", type="path", mountpoint=tmp_path / "metadata", subdirectory="", sentinel_id="metadata"),
        "index": TargetConfig(name="index", type="path", mountpoint=tmp_path / "index", subdirectory="", sentinel_id="index"),
    }
    return Config(
        appliance=ApplianceConfig(name="node-a", state_dir=tmp_path / "state", runtime_dir=tmp_path / "run", worker_fencing_mode="namespace"),
        targets=targets, metadata=MetadataConfig(), index=IndexConfig(), seaweed=SeaweedConfig(), s3=S3Config(),
    )


def write_failover(path: Path, *, verify: bool = True) -> None:
    path.write_text(f'''
[storage.data.failover]
enabled = true
primary_name = "cifs-primary"
failure_cooldown_seconds = 60
verify_all_transports_on_startup = {str(verify).lower()}

[[storage.data.failover.transports]]
name = "sshfs-secondary"
source = "user@host:/remote"
identity_file = "/run/secrets/key"
known_hosts_file = "/run/secrets/known_hosts"
''', encoding="utf-8")


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


def test_startup_verification_checks_every_transport_sequentially(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.generation_factory.create = Mock(return_value=fake_generation(1))
    guardian._begin_generation()
    calls: list[tuple[str, str, bool]] = []

    def helper(operation, target_name=None, *, full=False, timeout):
        calls.append((operation, guardian._startup_verification_transport, full))
        return {"transport_name": guardian._startup_verification_transport} if operation == "probe" else {}

    guardian._run_helper = Mock(side_effect=helper)
    guardian._verify_all_transports_on_startup()
    assert calls == [
        ("mount", "cifs-primary", False), ("prepare", "cifs-primary", False),
        ("probe", "cifs-primary", True), ("unmount", "cifs-primary", False),
        ("mount", "sshfs-secondary", False), ("prepare", "sshfs-secondary", False),
        ("probe", "sshfs-secondary", True), ("unmount", "sshfs-secondary", False),
    ]
    assert guardian._startup_verification_pending is False


def test_startup_verification_is_skipped_after_same_credentials_were_certified(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    config = make_config(tmp_path)
    first = Guardian(config, str(config_path))
    first.generation_factory.create = Mock(return_value=fake_generation(1))
    first._begin_generation()
    first._run_helper = Mock(return_value={})
    first._verify_all_transports_on_startup()
    second = Guardian(config, str(config_path))
    second.generation_factory.create = Mock(return_value=fake_generation(2))
    second._begin_generation()
    assert second._startup_verification_pending is False


def test_startup_verification_can_be_disabled_for_known_offline_fallback(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path, verify=False)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.generation_factory.create = Mock(return_value=fake_generation(1))
    guardian._begin_generation()
    assert guardian._startup_verification_pending is False
    guardian._run_helper = Mock()
    guardian._verify_all_transports_on_startup()
    guardian._run_helper.assert_not_called()


def test_startup_failure_is_attributed_to_the_transport_being_checked(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.active_transport = "cifs-primary"
    guardian._startup_verification_transport = "sshfs-secondary"

    def fail(*_args, **_kwargs):
        raise StorageError("bad password")

    monkeypatch.setattr(GenerationGuardian, "_run_helper", fail)
    with pytest.raises(StorageError, match="bad password"):
        guardian._run_helper("mount", "data", timeout=30)
    assert guardian._transport_failure_name == "sshfs-secondary"


def test_transport_failure_rotates_generation_to_secondary(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    first = fake_generation(1)
    second = fake_generation(2)
    guardian.generation_factory.create = Mock(side_effect=[first, second])
    guardian._begin_generation()
    guardian._transport_failure = True
    assert guardian._fence_generation("storage probe failed") is True
    guardian._begin_generation()
    assert guardian.active_transport == "sshfs-secondary"
    assert first.retire.called


def test_non_storage_failure_keeps_current_transport(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.generation_factory.create = Mock(side_effect=[fake_generation(1), fake_generation(2)])
    guardian._begin_generation()
    assert guardian._fence_generation("HAProxy exited") is True
    guardian._begin_generation()
    assert guardian.active_transport == "cifs-primary"


def test_fence_reaps_the_selected_sshfs_process_after_network_is_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.generation = fake_generation(1)
    guardian.active_transport = "sshfs-secondary"
    stopped: list[str] = []
    monkeypatch.setattr(
        "s3_storage_node.transport_guardian._stop_sshfs_process",
        lambda target: stopped.append(target.transport_name),
    )

    assert guardian._fence_generation("storage probe failed") is True
    assert guardian.generation.fence.called
    assert stopped == ["sshfs-secondary"]


def test_manual_switch_drains_and_detaches_before_fence(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.generation_factory.create = Mock(side_effect=[fake_generation(1), fake_generation(2)])
    order: list[str] = []
    guardian._stop_seaweed = Mock(side_effect=lambda **_kwargs: order.append("drain") or True)
    guardian._run_helper = Mock(side_effect=lambda *args, **kwargs: order.append("unmount") or {})
    guardian._begin_generation()
    guardian.transport_selector.request("sshfs-secondary")
    guardian._online_transport_watch = True
    with pytest.raises(TransportSwitchRequested):
        guardian._interruptible_sleep(1)
    assert order == []
    assert guardian._terminate_generation(
        "operator requested transport switch to sshfs-secondary",
        cause="operator_transport_switch",
        phase="ONLINE",
    ) is True
    assert order == ["drain", "unmount"]
    guardian._run_helper.assert_called_once()


def test_manual_request_is_not_consumed_while_old_process_lingers(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.generation_factory.create = Mock(side_effect=[fake_generation(1), fake_generation(2)])
    guardian._begin_generation()
    guardian.transport_selector.request("sshfs-secondary")
    blocked = Mock()
    blocked.name = "volume"
    blocked.running.return_value = True
    guardian.lingering_processes = [blocked]
    with pytest.raises(ProcessError):
        guardian._begin_generation()
    assert guardian.transport_selector.pending_request() == "sshfs-secondary"


def test_manual_request_is_not_consumed_while_helper_lingers(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_failover(config_path)
    guardian = Guardian(make_config(tmp_path), str(config_path))
    guardian.generation_factory.create = Mock(side_effect=[fake_generation(1), fake_generation(2)])
    guardian._begin_generation()
    guardian.transport_selector.request("sshfs-secondary")
    blocked = Mock()
    blocked.poll.return_value = None
    guardian.helper_children = [blocked]
    with pytest.raises(StorageError):
        guardian._begin_generation()
    assert guardian.transport_selector.pending_request() == "sshfs-secondary"
