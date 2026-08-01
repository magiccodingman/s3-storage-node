from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from s3_storage_node.config import TargetConfig
from s3_storage_node.storage import (
    MountInfo,
    StorageError,
    probe_target,
    verify_block_identity,
    verify_or_initialize_sentinel,
)


def make_target(tmp_path: Path, **overrides: object) -> TargetConfig:
    values: dict[str, object] = {
        "name": "data",
        "type": "path",
        "mountpoint": tmp_path,
        "subdirectory": "storage",
        "sentinel_id": "expected-node",
        "allow_initialize": False,
        "min_free_bytes": 0,
    }
    values.update(overrides)
    return TargetConfig(**values)


def write_sentinel(target: TargetConfig, sentinel_id: str | None = None) -> None:
    target.storage_root.mkdir(parents=True, exist_ok=True)
    (target.storage_root / target.sentinel_file).write_text(
        json.dumps({"sentinel_id": sentinel_id or target.sentinel_id}),
        encoding="utf-8",
    )


def test_wrong_cifs_source_is_rejected_before_enrollment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = make_target(
        tmp_path,
        type="cifs",
        mountpoint=tmp_path / "mount",
        subdirectory="",
        source="//expected/share",
    )
    target.mountpoint.mkdir()
    monkeypatch.setattr(
        "s3_storage_node.storage.find_mount",
        lambda _path: MountInfo(str(target.mountpoint), "cifs", "//wrong/share"),
    )

    with pytest.raises(StorageError, match="source mismatch"):
        verify_or_initialize_sentinel(target, 10001, 10001)


def test_wrong_cifs_filesystem_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = make_target(
        tmp_path,
        type="cifs",
        mountpoint=tmp_path / "mount",
        subdirectory="",
        source="//expected/share",
    )
    target.mountpoint.mkdir()
    monkeypatch.setattr(
        "s3_storage_node.storage.find_mount",
        lambda _path: MountInfo(str(target.mountpoint), "ext4", "//expected/share"),
    )

    with pytest.raises(StorageError, match="unexpected filesystem"):
        verify_or_initialize_sentinel(target, 10001, 10001)


def test_wrong_sentinel_identity_is_rejected(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    write_sentinel(target, "different-node")

    with pytest.raises(StorageError, match="sentinel mismatch"):
        verify_or_initialize_sentinel(target, 10001, 10001)


def test_missing_sentinel_fails_closed_without_initialization_permission(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    target.storage_root.mkdir(parents=True)

    with pytest.raises(StorageError, match="sentinel missing"):
        verify_or_initialize_sentinel(target, 10001, 10001)


def test_capacity_floor_withdraws_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = make_target(tmp_path, min_free_bytes=10_000)
    write_sentinel(target)
    monkeypatch.setattr(
        "s3_storage_node.storage.os.statvfs",
        lambda _path: SimpleNamespace(f_bavail=1, f_frsize=4096, f_blocks=100),
    )

    with pytest.raises(StorageError, match="free space below floor"):
        probe_target(target)


def test_full_probe_writes_reads_and_cleans_up_canary_file(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    write_sentinel(target)

    result = probe_target(target, full=True)

    assert result["path"] == str(target.storage_root)
    assert list(target.storage_root.glob(".s3-storage-node-probe-*")) == []


def test_block_uuid_mismatch_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = make_target(
        tmp_path,
        type="block",
        device="/dev/fake",
        expected_uuid="expected-uuid",
        expected_filesystem="xfs",
    )
    monkeypatch.setattr(
        "s3_storage_node.storage._run",
        lambda _command: SimpleNamespace(stdout="UUID=wrong-uuid\nTYPE=xfs\n"),
    )

    with pytest.raises(StorageError, match="device UUID mismatch"):
        verify_block_identity(target)


def test_block_filesystem_mismatch_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = make_target(
        tmp_path,
        type="block",
        device="/dev/fake",
        expected_uuid="expected-uuid",
        expected_filesystem="xfs",
    )
    monkeypatch.setattr(
        "s3_storage_node.storage._run",
        lambda _command: SimpleNamespace(stdout="UUID=expected-uuid\nTYPE=ext4\n"),
    )

    with pytest.raises(StorageError, match="filesystem mismatch"):
        verify_block_identity(target)
