from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from s3_storage_node.config_types import TargetConfig
from s3_storage_node import storage


def key_target(tmp_path: Path) -> TargetConfig:
    key = tmp_path / "key"
    known = tmp_path / "known"
    key.write_text("key")
    known.write_text("host key")
    return TargetConfig(
        name="data", type="sshfs", mountpoint=tmp_path / "mount", subdirectory="seaweedfs",
        sentinel_id="dataset", source="user@host:/remote", transport_name="sshfs-secondary",
        ssh_auth_mode="key", ssh_identity_file=str(key), ssh_known_hosts_file=str(known), ssh_port=23,
        mount_options=("IdentityFile=" + str(key), "UserKnownHostsFile=" + str(known), "sshfs_sync"),
    )


def password_target(tmp_path: Path, password: str = "super-secret") -> TargetConfig:
    known = tmp_path / "known"
    credentials = tmp_path / "credentials"
    known.write_text("host key")
    credentials.write_text(f"username=user\npassword={password}\ndomain=\n")
    return TargetConfig(
        name="data", type="sshfs", mountpoint=tmp_path / "mount", subdirectory="seaweedfs",
        sentinel_id="dataset", source="user@host:/remote", transport_name="sshfs-password",
        ssh_auth_mode="password", ssh_credentials_file=str(credentials), ssh_known_hosts_file=str(known),
        ssh_port=23, mount_options=("password_stdin", "UserKnownHostsFile=" + str(known), "sshfs_sync"),
    )


class CaptureStdin:
    def __init__(self):
        self.value = ""
        self.closed = False

    def write(self, value):
        self.value += value
        return len(value)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class FakeProcess:
    pid = 321
    returncode = None

    def __init__(self, stdin=None):
        self.stdin = stdin

    def poll(self):
        return None

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


def test_mount_sshfs_key_uses_resolved_safe_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = key_target(tmp_path)
    commands = []
    checks = iter([None, SimpleNamespace(filesystem="fuse.sshfs", source=value.source)])
    monkeypatch.setattr(storage, "find_mount", lambda _path: next(checks))
    monkeypatch.setattr(storage, "prepare_barrier", lambda _target: None)
    monkeypatch.setattr(storage.subprocess, "Popen", lambda command, **kwargs: commands.append((command, kwargs)) or FakeProcess())
    storage.mount_target(value)
    assert commands[0][0] == ["sshfs", "-f", "user@host:/remote", str(value.mountpoint), "-o", ",".join(value.mount_options)]
    assert commands[0][1]["stdin"] is storage.subprocess.DEVNULL


def test_mount_sshfs_password_is_written_only_to_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = password_target(tmp_path)
    commands = []
    stdin = CaptureStdin()
    checks = iter([None, SimpleNamespace(filesystem="fuse.sshfs", source=value.source)])
    monkeypatch.setattr(storage, "find_mount", lambda _path: next(checks))
    monkeypatch.setattr(storage, "prepare_barrier", lambda _target: None)
    monkeypatch.setattr(storage.subprocess, "Popen", lambda command, **kwargs: commands.append((command, kwargs)) or FakeProcess(stdin))
    storage.mount_target(value)
    assert commands[0][1]["stdin"] is storage.subprocess.PIPE
    assert "super-secret" not in " ".join(commands[0][0])
    assert stdin.value == "super-secret\n"
    assert stdin.closed is True


def test_password_username_must_match_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = password_target(tmp_path)
    Path(value.ssh_credentials_file).write_text("username=someone-else\npassword=secret\n")
    monkeypatch.setattr(storage, "find_mount", lambda _path: None)
    monkeypatch.setattr(storage, "prepare_barrier", lambda _target: None)
    with pytest.raises(storage.StorageError, match="does not match credentials username"):
        storage.mount_target(value)


def test_password_never_appears_in_sshfs_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = "do-not-leak-this"
    value = password_target(tmp_path, password=password)
    stdin = CaptureStdin()

    class FailedProcess(FakeProcess):
        returncode = 5

        def poll(self):
            return 5

    monkeypatch.setattr(storage, "find_mount", lambda _path: None)
    monkeypatch.setattr(storage, "prepare_barrier", lambda _target: None)

    def popen(_command, **kwargs):
        kwargs["stderr"].write(f"authentication failed for {password}".encode())
        return FailedProcess(stdin)

    monkeypatch.setattr(storage.subprocess, "Popen", popen)
    with pytest.raises(storage.StorageError) as captured:
        storage.mount_target(value)
    assert password not in str(captured.value)
    assert "<redacted>" in str(captured.value)


def test_sshfs_mount_identity_checks_fuse_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = key_target(tmp_path)
    monkeypatch.setattr(storage, "find_mount", lambda _path: SimpleNamespace(filesystem="fuse.sshfs", source="user@host:/remote"))
    storage._verify_mount_identity(value)


def test_sshfs_mount_identity_rejects_wrong_dataset_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = key_target(tmp_path)
    monkeypatch.setattr(storage, "find_mount", lambda _path: SimpleNamespace(filesystem="fuse.sshfs", source="user@host:/other"))
    with pytest.raises(storage.StorageError, match="source mismatch"):
        storage._verify_mount_identity(value)


def test_sshfs_probe_reports_single_failure_domain_and_auth_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = password_target(tmp_path)
    root = value.storage_root
    root.mkdir(parents=True)
    (root / value.sentinel_file).write_text('{"sentinel_id":"dataset"}')
    monkeypatch.setattr(storage, "_verify_mount_identity", lambda _target: None)
    result = storage.probe_target(value)
    assert result["transport_type"] == "sshfs"
    assert result["transport_name"] == "sshfs-password"
    assert result["ssh_auth_mode"] == "password"
    assert result["failure_domains"] == 1


def test_pid_reuse_does_not_kill_unrelated_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = replace(key_target(tmp_path), ssh_runtime_pid_file=str(tmp_path / "sshfs.pid"))
    Path(value.ssh_runtime_pid_file).write_text("321\n")
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
    assert not Path(value.ssh_runtime_pid_file).exists()


def test_sshfs_pid_file_is_preserved_when_process_cannot_be_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = replace(key_target(tmp_path), ssh_runtime_pid_file=str(tmp_path / "sshfs.pid"))
    pid_path = Path(value.ssh_runtime_pid_file)
    pid_path.write_text("321\n")
    original_read_bytes = Path.read_bytes
    original_exists = Path.exists

    def read_bytes(path: Path) -> bytes:
        if str(path) == "/proc/321/cmdline":
            return f"sshfs\0source\0{value.mountpoint}\0".encode()
        return original_read_bytes(path)

    def exists(path: Path) -> bool:
        if str(path) == "/proc/321":
            return True
        return original_exists(path)

    ticks = iter(range(20))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(storage.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(storage.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(storage.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(storage.StorageError, match="preserving its PID file"):
        storage._stop_sshfs_process(value)

    assert pid_path.exists()
    assert killed == [(321, storage.signal.SIGTERM), (321, storage.signal.SIGKILL)]


def test_sshfs_unmount_prefers_clean_detach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = key_target(tmp_path)
    mounted = SimpleNamespace(filesystem="fuse.sshfs", source=value.source)
    states = iter([mounted, None])
    commands: list[list[str]] = []
    monkeypatch.setattr(storage, "find_mount", lambda _path: next(states))
    monkeypatch.setattr(
        storage, "_unmount_command",
        lambda command: commands.append(command) or (True, ""),
    )
    monkeypatch.setattr(storage, "_stop_sshfs_process", lambda _target: None)
    monkeypatch.setattr(storage, "prepare_barrier", lambda _target: None)

    storage.unmount_target(value)

    assert commands == [["fusermount3", "-u", str(value.mountpoint)]]


def test_sshfs_unmount_uses_lazy_detach_only_after_clean_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = key_target(tmp_path)
    mounted = SimpleNamespace(filesystem="fuse.sshfs", source=value.source)
    states = iter([mounted, None])
    commands: list[list[str]] = []

    def unmount(command: list[str]) -> tuple[bool, str]:
        commands.append(command)
        return (False, "busy") if "-z" not in command else (True, "")

    monkeypatch.setattr(storage, "find_mount", lambda _path: next(states))
    monkeypatch.setattr(storage, "_unmount_command", unmount)
    monkeypatch.setattr(storage, "_stop_sshfs_process", lambda _target: None)
    monkeypatch.setattr(storage, "prepare_barrier", lambda _target: None)

    storage.unmount_target(value)

    assert commands == [
        ["fusermount3", "-u", str(value.mountpoint)],
        ["fusermount3", "-u", "-z", str(value.mountpoint)],
    ]
