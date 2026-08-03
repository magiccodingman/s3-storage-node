from __future__ import annotations

from pathlib import Path

import pytest

from s3_storage_node.config_helpers import ConfigError
from s3_storage_node.config_target import _parse_target
from s3_storage_node.config_types import TargetConfig
from s3_storage_node.storage import (
    MountInfo,
    StorageError,
    _append_durability_probe,
    _cifs_mount_profiles,
    certify_cifs_transport,
    parse_cifs_debug_data,
    parse_cifs_stats,
    read_mountinfo,
)


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


def test_legacy_transport_options_are_canonicalized(tmp_path: Path) -> None:
    target = _parse_target(
        "data",
        raw_target(
            tmp_path,
            mount_options=["vers=3.1.1", "persistenthandles", "multichannel", "max_channels=4"],
        ),
        tmp_path,
    )
    assert target.handle_reconnect_policy == "persistent"
    assert target.multichannel_policy == "required"
    assert target.max_channels == 4
    assert target.mount_options == ("vers=3.1.1",)


def test_conflicting_transport_options_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="contradictory multichannel"):
        _parse_target(
            "data",
            raw_target(tmp_path, mount_options=["multichannel", "nomultichannel"]),
            tmp_path,
        )
    with pytest.raises(ConfigError, match="conflicts with legacy"):
        _parse_target(
            "data",
            raw_target(tmp_path, handle_reconnect_policy="disabled", mount_options=["persistenthandles"]),
            tmp_path,
        )


def test_auto_profiles_fall_back_from_strongest_capabilities(tmp_path: Path) -> None:
    target = _parse_target(
        "data",
        raw_target(tmp_path, handle_reconnect_policy="auto", multichannel_policy="auto", max_channels=4),
        tmp_path,
    )
    profiles = _cifs_mount_profiles(target)
    assert "persistenthandles" in profiles[0]
    assert "multichannel" in profiles[0]
    assert profiles[-1] == ("soft",)


def test_mountinfo_parser_exposes_effective_options(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "36 25 0:31 / /run/node rw,relatime - cifs //server/share rw,vers=3.1.1,persistenthandles,multichannel,max_channels=4\n",
        encoding="utf-8",
    )
    entry = read_mountinfo(str(mountinfo))[0]
    assert entry.mount_options == frozenset({"rw", "relatime"})
    assert "persistenthandles" in entry.super_options
    assert "max_channels=4" in entry.super_options


def test_debug_and_stats_parsers_extract_transport_signals() -> None:
    debug = parse_cifs_debug_data(
        """CIFS Version 2.45
1) Name: server Uses: 1
Dialect 0x311
TCP status: 1
Allocated channels: 4
[CONNECTED]
[CONNECTED]
Shares:
1) \\\\server\\share Mounts: 1
Status: 1 type: DISK
"""
    )
    assert debug["client_version"] == "2.45"
    assert debug["dialect"] == "3.1.1"
    assert debug["allocated_channels"] == 4
    assert debug["connected_channels"] == 2
    assert debug["shares"] == [{"source": "\\\\server\\share", "status": 1}]
    assert parse_cifs_stats("7 session 11 share reconnects\n") == {
        "session_reconnects": 7,
        "share_reconnects": 11,
    }


def test_transport_certification_enforces_dialect_and_required_features(tmp_path: Path) -> None:
    target = TargetConfig(
        name="data",
        type="cifs",
        mountpoint=tmp_path,
        subdirectory="",
        sentinel_id="dataset-1",
        source="//server/share",
        credentials_file="/secret",
        minimum_smb_dialect="3.1.1",
        handle_reconnect_policy="persistent",
        multichannel_policy="required",
        max_channels=4,
        require_transport_observability=True,
    )
    debug = tmp_path / "DebugData"
    debug.write_text(
        """CIFS Version 2.45
Dialect 0x311
Allocated channels: 4
[CONNECTED]
[CONNECTED]
1) \\\\server\\share Mounts: 1
Status: 1 type: DISK
""",
        encoding="utf-8",
    )
    stats = tmp_path / "Stats"
    stats.write_text("2 session 3 share reconnects\n", encoding="utf-8")
    mount = MountInfo(
        mountpoint=str(tmp_path),
        filesystem="cifs",
        source="//server/share",
        super_options=frozenset({"rw", "vers=3.1.1", "persistenthandles", "multichannel", "max_channels=4"}),
    )
    result = certify_cifs_transport(
        target,
        mount=mount,
        debug_data_path=str(debug),
        stats_path=str(stats),
    )
    assert result["effective_smb_dialect"] == "3.1.1"
    assert result["effective_handle_reconnect_mode"] == "persistent"
    assert result["connected_channels"] == 2
    assert result["cifs_share_reconnects"] == 3


def test_transport_certification_fails_closed_when_required_state_is_missing(tmp_path: Path) -> None:
    target = TargetConfig(
        name="data",
        type="cifs",
        mountpoint=tmp_path,
        subdirectory="",
        sentinel_id="dataset-1",
        source="//server/share",
        credentials_file="/secret",
        handle_reconnect_policy="persistent",
    )
    mount = MountInfo(
        mountpoint=str(tmp_path),
        filesystem="cifs",
        source="//server/share",
        super_options=frozenset({"vers=3.1.1"}),
    )
    with pytest.raises(StorageError, match="persistent handles"):
        certify_cifs_transport(target, mount=mount, debug_data_path=str(tmp_path / "missing"))


def test_persistent_append_probe_is_bounded_and_verifiable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("s3_storage_node.durability._DURABILITY_PROBE_MAX_BYTES", 5000)
    first = _append_durability_probe(tmp_path)
    second = _append_durability_probe(tmp_path)
    assert first["durability_probe_bytes"] == 4096
    assert second["durability_probe_size_bytes"] < 5000
    assert (tmp_path / ".s3-storage-node-durability-probe").exists()
