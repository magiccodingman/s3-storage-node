from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from s3_storage_node.config_helpers import ConfigError
from s3_storage_node.config_target import _parse_target
from s3_storage_node.config_types import TargetConfig
from s3_storage_node.storage import StorageError, mount_target, probe_target


def raw_target(runtime: Path, **overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "type": "cifs",
        "source": "//server/share",
        "credentials_file": "/run/secrets/cifs",
        "mountpoint": str(runtime / "mounts" / "data"),
        "sentinel_id": "dataset-1",
    }
    raw.update(overrides)
    return raw


def test_cifs_policy_defaults_explicitly_to_soft(tmp_path: Path) -> None:
    target = _parse_target("data", raw_target(tmp_path), tmp_path)
    assert target.io_failure_policy == "soft"
    assert "soft" not in target.mount_options


def test_legacy_hard_mount_option_is_canonicalized(tmp_path: Path) -> None:
    target = _parse_target("data", raw_target(tmp_path, mount_options=["vers=3.1.1", "hard"]), tmp_path)
    assert target.io_failure_policy == "hard"
    assert target.mount_options == ("vers=3.1.1",)


def test_conflicting_explicit_and_legacy_policy_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="conflicts"):
        _parse_target(
            "data",
            raw_target(tmp_path, io_failure_policy="soft", mount_options=["hard"]),
            tmp_path,
        )


def test_both_legacy_policies_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="both soft and hard"):
        _parse_target("data", raw_target(tmp_path, mount_options=["soft", "hard"]), tmp_path)


def test_invalid_policy_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be soft or hard"):
        _parse_target("data", raw_target(tmp_path, io_failure_policy="eventually"), tmp_path)


def test_policy_is_rejected_for_non_cifs_targets(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="only valid for CIFS"):
        _parse_target(
            "data",
            {
                "type": "path",
                "mountpoint": str(tmp_path / "path"),
                "sentinel_id": "path-1",
                "io_failure_policy": "soft",
            },
            tmp_path,
        )


def test_mount_command_always_contains_one_explicit_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = TargetConfig(
        name="data",
        type="cifs",
        mountpoint=tmp_path / "mount",
        subdirectory="",
        sentinel_id="dataset-1",
        source="//server/share",
        credentials_file="/run/secrets/cifs",
        mount_options=("vers=3.1.1",),
        io_failure_policy="hard",
    )
    commands: list[list[str]] = []
    monkeypatch.setattr("s3_storage_node.storage.prepare_barrier", lambda _target: None)
    monkeypatch.setattr(
        "s3_storage_node.storage._run",
        lambda command, timeout=20: commands.append(command) or SimpleNamespace(returncode=0),
    )
    mount_target(target)
    options = commands[0][-1].split(",")
    assert options.count("hard") == 1
    assert "soft" not in options


def test_raw_policy_in_target_is_defensively_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = TargetConfig(
        name="data",
        type="cifs",
        mountpoint=tmp_path / "mount",
        subdirectory="",
        sentinel_id="dataset-1",
        source="//server/share",
        credentials_file="/run/secrets/cifs",
        mount_options=("soft",),
        io_failure_policy="soft",
    )
    monkeypatch.setattr("s3_storage_node.storage.prepare_barrier", lambda _target: None)
    with pytest.raises(StorageError, match="use io_failure_policy"):
        mount_target(target)


def test_probe_reports_configured_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    target = TargetConfig(
        name="data",
        type="cifs",
        mountpoint=root,
        subdirectory="",
        sentinel_id="dataset-1",
        source="//server/share",
        credentials_file="/run/secrets/cifs",
        io_failure_policy="hard",
        min_free_bytes=0,
    )
    (root / target.sentinel_file).write_text(json.dumps({"sentinel_id": "dataset-1"}), encoding="utf-8")
    monkeypatch.setattr("s3_storage_node.storage._verify_mount_identity", lambda _target: None)
    result = probe_target(target)
    assert result["configured_io_failure_policy"] == "hard"
