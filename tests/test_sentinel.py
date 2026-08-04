from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from s3_storage_node.config_types import TargetConfig
from s3_storage_node.sentinel import (
    CURRENT_SENTINEL_VERSION,
    SENTINEL_SCHEMA,
    SentinelError,
    sentinel_v2_payload,
    validate_sentinel_payload,
)
from s3_storage_node.storage import probe_target, verify_or_initialize_sentinel


def target(tmp_path: Path, **changes) -> TargetConfig:
    base = TargetConfig(
        name="data",
        type="path",
        mountpoint=tmp_path,
        subdirectory="seaweedfs",
        sentinel_id="dataset-a",
        allow_initialize=True,
        min_free_bytes=0,
        transport_name="cifs-primary",
    )
    return replace(base, **changes)


def test_legacy_v1_sentinel_remains_valid(tmp_path: Path) -> None:
    configured = target(tmp_path)
    identity = validate_sentinel_payload(
        configured,
        {"version": 1, "sentinel_id": "dataset-a", "target": "data"},
    )
    assert identity.version == 1
    assert identity.legacy is True
    assert identity.dataset_id == "dataset-a"


def test_missing_version_is_treated_as_legacy_v1(tmp_path: Path) -> None:
    identity = validate_sentinel_payload(target(tmp_path), {"sentinel_id": "dataset-a"})
    assert identity.version == 1
    assert identity.schema == "legacy-v1"


def test_v2_is_transport_independent(tmp_path: Path) -> None:
    cifs = target(tmp_path, type="cifs", transport_name="cifs-primary")
    sshfs = target(
        tmp_path,
        type="sshfs",
        source="root@example:/srv/storage",
        transport_name="sshfs-secondary",
    )
    payload = sentinel_v2_payload(cifs)
    assert "transport" not in payload
    assert "source" not in payload
    assert validate_sentinel_payload(cifs, payload).version == CURRENT_SENTINEL_VERSION
    assert validate_sentinel_payload(sshfs, payload).dataset_id == "dataset-a"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", "wrong", "schema"),
        ("sentinel_id", "wrong", "dataset mismatch"),
        ("dataset_id", "wrong", "dataset mismatch"),
        ("target", "metadata", "target mismatch"),
        ("subdirectory", "other", "subdirectory mismatch"),
        ("transport_independent", False, "transport_independent"),
    ],
)
def test_v2_rejects_identity_mismatches(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    configured = target(tmp_path)
    payload = sentinel_v2_payload(configured)
    payload[field] = value
    with pytest.raises(SentinelError, match=message):
        validate_sentinel_payload(configured, payload)


def test_unknown_sentinel_version_fails_closed(tmp_path: Path) -> None:
    payload = sentinel_v2_payload(target(tmp_path))
    payload["version"] = 3
    with pytest.raises(SentinelError, match="unsupported sentinel version"):
        validate_sentinel_payload(target(tmp_path), payload)


def test_new_enrollment_writes_v2_and_probe_reports_identity(tmp_path: Path) -> None:
    configured = target(tmp_path)
    verify_or_initialize_sentinel(configured, os.getuid(), os.getgid())
    payload = json.loads((configured.storage_root / configured.sentinel_file).read_text(encoding="utf-8"))
    assert payload == {
        "schema": SENTINEL_SCHEMA,
        "version": CURRENT_SENTINEL_VERSION,
        "sentinel_id": "dataset-a",
        "dataset_id": "dataset-a",
        "target": "data",
        "subdirectory": "seaweedfs",
        "transport_independent": True,
    }
    result = probe_target(configured)
    assert result["sentinel_version"] == 2
    assert result["sentinel_schema"] == SENTINEL_SCHEMA
    assert result["dataset_id"] == "dataset-a"


def test_existing_v1_is_not_silently_rewritten(tmp_path: Path) -> None:
    configured = target(tmp_path)
    configured.storage_root.mkdir(parents=True)
    sentinel = configured.storage_root / configured.sentinel_file
    original = {"version": 1, "sentinel_id": "dataset-a", "target": "data"}
    sentinel.write_text(json.dumps(original) + "\n", encoding="utf-8")

    verify_or_initialize_sentinel(configured, os.getuid(), os.getgid())

    assert json.loads(sentinel.read_text(encoding="utf-8")) == original
    result = probe_target(configured)
    assert result["sentinel_version"] == 1
    assert result["sentinel_schema"] == "legacy-v1"
