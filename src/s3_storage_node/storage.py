from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Iterable

from .cifs_transport import (
    CifsTransportError,
    cifs_mount_options as _cifs_mount_options,
    cifs_mount_profiles as _cifs_mount_profiles,
    certify_cifs_transport as _certify_cifs_transport,
    is_capability_failure,
    parse_cifs_debug_data,
    parse_cifs_stats,
)
from .config_types import TargetConfig
from .durability import append_durability_probe as _append_durability_probe
from .durability import fsync_directory, temporary_durability_probe
from .mounts import MountInfo, decode_mountinfo_path, find_mount, read_mountinfo


class StorageError(RuntimeError):
    pass


def certify_cifs_transport(*args, **kwargs):
    try:
        return _certify_cifs_transport(*args, **kwargs)
    except CifsTransportError as exc:
        raise StorageError(str(exc)) from exc


def _run(command: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise StorageError(f"command failed: {' '.join(command)}: {message}")
    return result


def prepare_barrier(target: TargetConfig) -> None:
    if target.type == "path":
        return
    target.mountpoint.mkdir(parents=True, exist_ok=True)
    os.chown(target.mountpoint, 0, 0)
    os.chmod(target.mountpoint, 0)


def verify_block_identity(target: TargetConfig) -> None:
    result = _run(["blkid", "-o", "export", target.device])
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if values.get("UUID") != target.expected_uuid:
        raise StorageError(
            f"device UUID mismatch for {target.name}: expected {target.expected_uuid}, "
            f"got {values.get('UUID', '<none>')}"
        )
    if values.get("TYPE") != target.expected_filesystem:
        raise StorageError(
            f"filesystem mismatch for {target.name}: expected {target.expected_filesystem}, "
            f"got {values.get('TYPE', '<none>')}"
        )


def _mount_cifs(target: TargetConfig) -> None:
    try:
        profiles = _cifs_mount_profiles(target)
    except CifsTransportError as exc:
        raise StorageError(str(exc)) from exc
    last_error: StorageError | None = None
    for index, profile in enumerate(profiles):
        options = [f"credentials={target.credentials_file}", *profile]
        try:
            _run(
                ["mount", "-t", "cifs", target.source, str(target.mountpoint), "-o", ",".join(options)],
                timeout=30,
            )
            return
        except StorageError as exc:
            last_error = exc
            if index == len(profiles) - 1 or not is_capability_failure(exc):
                raise
    assert last_error is not None
    raise last_error


def _sshfs_pid_path(target: TargetConfig) -> Path | None:
    return Path(target.ssh_runtime_pid_file) if target.ssh_runtime_pid_file else None


def _stop_sshfs_process(target: TargetConfig) -> None:
    pid_path = _sshfs_pid_path(target)
    if pid_path is None:
        return
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, OSError):
        pid_path.unlink(missing_ok=True)
        return
    executable = Path(command[0].decode(errors="replace")).name if command and command[0] else ""
    arguments = {item.decode(errors="replace") for item in command[1:] if item}
    if executable != "sshfs" or str(target.mountpoint) not in arguments:
        pid_path.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
        time.sleep(0.05)
    if Path(f"/proc/{pid}").exists():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    pid_path.unlink(missing_ok=True)


def _mount_sshfs(target: TargetConfig) -> None:
    if not target.ssh_identity_file or not target.ssh_known_hosts_file:
        raise StorageError(f"SSHFS transport {target.transport_name or target.name} is missing identity configuration")
    for path, label in (
        (target.ssh_identity_file, "identity file"),
        (target.ssh_known_hosts_file, "known-hosts file"),
    ):
        if not Path(path).is_file():
            raise StorageError(f"SSHFS {label} does not exist: {path}")
    runtime_identity = Path(target.ssh_runtime_identity_file or target.ssh_identity_file)
    if runtime_identity != Path(target.ssh_identity_file):
        runtime_identity.parent.mkdir(parents=True, exist_ok=True)
        temporary = runtime_identity.with_suffix(runtime_identity.suffix + ".tmp")
        temporary.write_bytes(Path(target.ssh_identity_file).read_bytes())
        os.chmod(temporary, 0o600)
        temporary.replace(runtime_identity)
    command = ["sshfs", "-f", target.source, str(target.mountpoint)]
    if target.mount_options:
        command.extend(["-o", ",".join(target.mount_options)])
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise StorageError(
                    f"SSHFS transport {target.transport_name or target.name} exited with code {process.returncode}"
                )
            if find_mount(target.mountpoint) is not None:
                pid_path = _sshfs_pid_path(target)
                if pid_path is not None:
                    pid_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = pid_path.with_suffix(pid_path.suffix + ".tmp")
                    temporary.write_text(f"{process.pid}\n", encoding="ascii")
                    os.chmod(temporary, 0o600)
                    temporary.replace(pid_path)
                return
            time.sleep(0.1)
        raise StorageError(f"timed out waiting for SSHFS transport {target.transport_name or target.name}")
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        raise


def mount_target(target: TargetConfig) -> None:
    if target.type == "path":
        if not target.mountpoint.exists():
            raise StorageError(f"path target does not exist: {target.mountpoint}")
        return
    if find_mount(target.mountpoint):
        return

    prepare_barrier(target)
    if target.type == "cifs":
        _mount_cifs(target)
    elif target.type == "sshfs":
        _mount_sshfs(target)
    elif target.type == "block":
        verify_block_identity(target)
        command = ["mount", "-t", target.expected_filesystem]
        if target.mount_options:
            command.extend(["-o", ",".join(target.mount_options)])
        command.extend([target.device, str(target.mountpoint)])
        _run(command, timeout=30)
    else:
        raise StorageError(f"unsupported storage type: {target.type}")


def unmount_target(target: TargetConfig) -> None:
    if target.type == "path":
        return
    if find_mount(target.mountpoint) is None:
        if target.type == "sshfs":
            _stop_sshfs_process(target)
        prepare_barrier(target)
        return
    if target.type == "sshfs":
        result = subprocess.run(
            ["fusermount3", "-u", "-z", str(target.mountpoint)],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            _stop_sshfs_process(target)
            prepare_barrier(target)
            return
    result = subprocess.run(
        ["umount", "-l", str(target.mountpoint)],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise StorageError(result.stderr.strip() or f"failed to unmount {target.mountpoint}")
    if target.type == "sshfs":
        _stop_sshfs_process(target)
    prepare_barrier(target)


def _verify_mount_identity(target: TargetConfig) -> None:
    if target.type == "path":
        return
    mount = find_mount(target.mountpoint)
    if mount is None:
        raise StorageError(f"{target.name} is not mounted")
    if target.type == "cifs":
        if mount.filesystem not in {"cifs", "smb3"}:
            raise StorageError(f"{target.name} mounted with unexpected filesystem {mount.filesystem}")
        expected = target.source.rstrip("/")
        if mount.source.rstrip("/") != expected:
            raise StorageError(f"{target.name} source mismatch: expected {expected}, got {mount.source}")
    elif target.type == "sshfs":
        if mount.filesystem not in {"fuse.sshfs", "fuse"}:
            raise StorageError(f"{target.name} mounted with unexpected filesystem {mount.filesystem}")
        if mount.source.rstrip("/") != target.source.rstrip("/"):
            raise StorageError(f"{target.name} source mismatch: expected {target.source}, got {mount.source}")
    elif target.type == "block" and mount.filesystem != target.expected_filesystem:
        raise StorageError(
            f"{target.name} filesystem mismatch: expected {target.expected_filesystem}, got {mount.filesystem}"
        )


def _sentinel_path(target: TargetConfig) -> Path:
    return target.storage_root / target.sentinel_file


def verify_or_initialize_sentinel(target: TargetConfig, uid: int, gid: int) -> None:
    _verify_mount_identity(target)
    if target.type == "cifs":
        certify_cifs_transport(target)
    if not target.storage_root.exists():
        if not target.allow_initialize:
            raise StorageError(
                f"storage root missing for {target.name}; set allow_initialize=true only for the first intentional enrollment"
            )
        target.storage_root.mkdir(parents=True, exist_ok=True)

    sentinel = _sentinel_path(target)
    if sentinel.exists():
        try:
            payload = json.loads(sentinel.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"invalid sentinel on {target.name}: {exc}") from exc
        if payload.get("sentinel_id") != target.sentinel_id:
            raise StorageError(
                f"sentinel mismatch for {target.name}: expected {target.sentinel_id}, "
                f"got {payload.get('sentinel_id', '<none>')}"
            )
    elif target.allow_initialize:
        payload = {"sentinel_id": target.sentinel_id, "target": target.name, "version": 1}
        temporary = sentinel.with_suffix(sentinel.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        fd = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        temporary.replace(sentinel)
        fsync_directory(target.storage_root)
    else:
        raise StorageError(
            f"sentinel missing for {target.name}; set allow_initialize=true only for the first intentional enrollment"
        )

    try:
        os.chown(target.storage_root, uid, gid)
    except OSError:
        pass
    try:
        os.chmod(target.storage_root, 0o770)
    except OSError:
        pass


def prepare_role_paths(paths: Iterable[Path], uid: int, gid: int) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chown(path, uid, gid)
        except OSError:
            pass
        try:
            os.chmod(path, 0o770)
        except OSError:
            pass


def probe_target(target: TargetConfig, full: bool = False) -> dict[str, int | str]:
    _verify_mount_identity(target)
    sentinel = _sentinel_path(target)
    payload = json.loads(sentinel.read_text(encoding="utf-8"))
    if payload.get("sentinel_id") != target.sentinel_id:
        raise StorageError(f"sentinel mismatch for {target.name}")

    stats = os.statvfs(target.storage_root)
    free_bytes = stats.f_bavail * stats.f_frsize
    total_bytes = stats.f_blocks * stats.f_frsize
    if free_bytes < target.min_free_bytes:
        raise StorageError(f"{target.name} free space below floor: {free_bytes} < {target.min_free_bytes}")

    result: dict[str, int | str] = {
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "path": str(target.storage_root),
        "transport_type": target.type,
        "transport_name": target.transport_name or target.type,
        "failure_domains": 1,
        "durability_class": "transport_acknowledged",
    }
    if target.type == "cifs":
        result.update(certify_cifs_transport(target))
    elif target.type == "sshfs":
        result.update({
            "sshfs_sync": 1 if "sshfs_sync" in target.mount_options else 0,
            "transport_observed": 1,
        })
    if full:
        try:
            result.update(_append_durability_probe(target.storage_root))
            temporary_durability_probe(target.storage_root)
        except OSError as exc:
            raise StorageError(str(exc)) from exc
    return result
