from __future__ import annotations

import errno
import hashlib
import os
import struct
import tempfile
from pathlib import Path


_DURABILITY_MAGIC = b"S3SNPRB1"
_DURABILITY_HEADER = struct.Struct(">8sI32s")
_DURABILITY_PROBE_NAME = ".s3-storage-node-durability-probe"
_DURABILITY_PROBE_MAX_BYTES = 1_048_576


def fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
    finally:
        os.close(directory_fd)


def append_durability_probe(root: Path) -> dict[str, int]:
    path = root / _DURABILITY_PROBE_NAME
    payload = os.urandom(4096)
    record = _DURABILITY_HEADER.pack(_DURABILITY_MAGIC, len(payload), hashlib.sha256(payload).digest()) + payload
    with open(path, "ab") as handle:
        handle.write(record)
        handle.flush()
        os.fsync(handle.fileno())
    with open(path, "rb") as handle:
        handle.seek(-len(record), os.SEEK_END)
        observed = handle.read(len(record))
    if observed != record:
        raise OSError(f"persistent append read-back mismatch at {root}")

    size = path.stat().st_size
    if size > _DURABILITY_PROBE_MAX_BYTES:
        temporary = path.with_suffix(".compact.tmp")
        try:
            with open(temporary, "wb") as handle:
                handle.write(record)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            fsync_directory(root)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        size = path.stat().st_size
    return {"durability_probe_bytes": len(payload), "durability_probe_size_bytes": size}


def temporary_durability_probe(root: Path) -> None:
    fd, filename = tempfile.mkstemp(prefix=".s3-storage-node-probe-", dir=root)
    data = os.urandom(4096)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with open(filename, "rb") as handle:
            if handle.read() != data:
                raise OSError(f"read-back mismatch at {root}")
        os.unlink(filename)
        fsync_directory(root)
    finally:
        try:
            os.unlink(filename)
        except FileNotFoundError:
            pass
