from __future__ import annotations

import errno
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import TargetConfig


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class MountInfo:
    mountpoint: str
    filesystem: str
    source: str


def decode_mountinfo_path(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def read_mountinfo(path: str = "/proc/self/mountinfo") -> list[MountInfo]:
    entries: list[MountInfo] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            before, separator, after = line.rstrip("\n").partition(" - ")
            if not separator:
                continue
            left = before.split()
            right = after.split()
            if len(left) < 5 or len(right) < 2:
                continue
            entries.append(MountInfo(
                mountpoint=decode_mountinfo_path(left[4]),
                filesystem=right[0],
                source=decode_mountinfo_path(right[1]),
            ))
    return entries


def find_mount(path: Path, entries: Iterable[MountInfo] | None = None) -> MountInfo | None:
    wanted = os.path.realpath(path)
    for entry in entries or read_mountinfo():
        if os.path.realpath(entry.mountpoint) == wanted:
            return entry
    return None


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
        raise StorageError(f"device UUID mismatch for {target.name}: expected {target.expected_uuid}, got {values.get('UUID', '<none>')}")
    if values.get("TYPE") != target.expected_filesystem:
        raise StorageError(f"filesystem mismatch for {target.name}: expected {target.expected_filesystem}, got {values.get('TYPE', '<none>')}")


def _cifs_mount_options(target: TargetConfig) -> tuple[str, ...]:
    policy = target.effective_io_failure_policy
    if policy not in {"soft", "hard"}:
        raise StorageError(f"invalid CIFS I/O failure policy for {target.name}: {policy}")
    conflicting = [
        option
        for option in target.mount_options
        if option.partition("=")[0].strip().lower() in {"soft", "hard"}
    ]
    if conflicting:
        raise StorageError(
            f"raw CIFS I/O failure options remain for {target.name}; use io_failure_policy instead"
        )
    return (policy, *target.mount_options)


def mount_target(target: TargetConfig) -> None:
    if target.type == "path":
        if not target.mountpoint.exists():
            raise StorageError(f"path target does not exist: {target.mountpoint}")
        return

    existing = find_mount(target.mountpoint)
    if existing:
        return

    prepare_barrier(target)
    if target.type == "cifs":
        options = [f"credentials={target.credentials_file}", *_cifs_mount_options(target)]
        _run(["mount", "-t", "cifs", target.source, str(target.mountpoint), "-o", ",".join(options)], timeout=30)
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
        prepare_barrier(target)
        return
    result = subprocess.run(["umount", "-l", str(target.mountpoint)], text=True, capture_output=True, timeout=15, check=False)
    if result.returncode != 0:
        raise StorageError(result.stderr.strip() or f"failed to unmount {target.mountpoint}")
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
    elif target.type == "block" and mount.filesystem != target.expected_filesystem:
        raise StorageError(f"{target.name} filesystem mismatch: expected {target.expected_filesystem}, got {mount.filesystem}")


def _sentinel_path(target: TargetConfig) -> Path:
    return target.storage_root / target.sentinel_file


def verify_or_initialize_sentinel(target: TargetConfig, uid: int, gid: int) -> None:
    _verify_mount_identity(target)
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
                f"sentinel mismatch for {target.name}: expected {target.sentinel_id}, got {payload.get('sentinel_id', '<none>')}"
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
        directory_fd = os.open(target.storage_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    raise
        finally:
            os.close(directory_fd)
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

    if full:
        fd, filename = tempfile.mkstemp(prefix=".s3-storage-node-probe-", dir=target.storage_root)
        data = os.urandom(4096)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            with open(filename, "rb") as handle:
                if handle.read() != data:
                    raise StorageError(f"read-back mismatch on {target.name}")
            os.unlink(filename)
            directory_fd = os.open(target.storage_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                try:
                    os.fsync(directory_fd)
                except OSError as exc:
                    if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                        raise
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(filename)
            except FileNotFoundError:
                pass

    result: dict[str, int | str] = {
        "free_bytes": free_bytes,
        "total_bytes": total_bytes,
        "path": str(target.storage_root),
    }
    if target.type == "cifs":
        result["configured_io_failure_policy"] = target.effective_io_failure_policy
    return result
