from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from s3_storage_node.config_types import TargetConfig
from s3_storage_node import storage


def target(tmp_path: Path) -> TargetConfig:
    key = tmp_path / "key"
    known = tmp_path / "known"
    key.write_text("key")
    known.write_text("host key")
    return TargetConfig(
        name="data",
        type="sshfs",
        mountpoint=tmp_path / "mount",
        subdirectory="seaweedfs",
        sentinel_id="dataset",
        source="user@host:/remote",
        transport_name="sshfs-secondary",
        ssh_identity_file=str(key),
        ssh_known_hosts_file=str(known),
        ssh_port=23,
        mount_options=("IdentityFile=" + str(key), "UserKnownHostsFile=" + str(known), "sshfs_sync"),
    )


def test_mount_sshfs_uses_resolved_safe_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = target(tmp_path)
    commands = []
    checks = iter([None, SimpleNamespace(filesystem="fuse.sshfs", source=value.source)])
    monkeypatch.setattr(storage, "find_mount", lambda _path: next(checks))
    monkeypatch.setattr(storage, "prepare_barrier", lambda _target: None)

    class FakeProcess:
        pid = 321
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr(storage.subprocess, "Popen", lambda command, **_kwargs: commands.append(command) or FakeProcess())
    storage.mount_target(value)
    assert commands == [[
        "sshfs", "-f", "user@host:/remote", str(value.mountpoint), "-o", ",".join(value.mount_options),
    ]]


def test_sshfs_mount_identity_checks_fuse_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = target(tmp_path)
    monkeypatch.setattr(
        storage,
        "find_mount",
        lambda _path: SimpleNamespace(filesystem="fuse.sshfs", source="user@host:/remote"),
    )
    storage._verify_mount_identity(value)


def test_sshfs_mount_identity_rejects_wrong_dataset_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = target(tmp_path)
    monkeypatch.setattr(
        storage,
        "find_mount",
        lambda _path: SimpleNamespace(filesystem="fuse.sshfs", source="user@host:/other"),
    )
    with pytest.raises(storage.StorageError, match="source mismatch"):
        storage._verify_mount_identity(value)


def test_sshfs_probe_reports_single_failure_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = target(tmp_path)
    root = value.storage_root
    root.mkdir(parents=True)
    (root / value.sentinel_file).write_text('{"sentinel_id":"dataset"}')
    monkeypatch.setattr(storage, "_verify_mount_identity", lambda _target: None)
    result = storage.probe_target(value)
    assert result["transport_type"] == "sshfs"
    assert result["transport_name"] == "sshfs-secondary"
    assert result["failure_domains"] == 1
    assert result["durability_class"] == "transport_acknowledged"


def test_pid_reuse_does_not_kill_unrelated_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = target(tmp_path)
    pid_file = tmp_path / "sshfs.pid"
    value = replace(value, ssh_runtime_pid_file=str(pid_file))
    pid_file.write_text("321\n")
    original = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if str(path) == "/proc/321/cmdline":
            return b"sleep\x001000\x00"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    killed = []
    monkeypatch.setattr(storage.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    storage._stop_sshfs_process(value)
    assert killed == []
    assert not pid_file.exists()
