from __future__ import annotations

import struct
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import s3_storage_node.index_repair_helper as repair_helper
from s3_storage_node.index_repair_helper import RepairHelperError, fingerprint, inspect_index_bounds


def _dat(path: Path, size: int = 128, version: int = 3) -> Path:
    path.write_bytes(bytes([version]) + bytes(size - 1))
    return path


def _idx(path: Path, *entries: tuple[int, int, int]) -> Path:
    path.write_bytes(b"".join(
        struct.pack(">QIi", needle_id, offset // 8, size)
        for needle_id, offset, size in entries
    ))
    return path


def test_index_bounds_accepts_entries_contained_in_dat(tmp_path: Path) -> None:
    source = _dat(tmp_path / "volume.dat")
    candidate = _idx(tmp_path / "volume.idx", (7, 8, 1), (8, 40, 9))

    result = inspect_index_bounds(candidate, source)

    assert result["valid"] is True
    assert result["entry_count"] == 2
    assert result["maximum_needle_end"] == 80
    assert result["violations"] == []


def test_index_bounds_reports_weed_fix_partial_tail_entry(tmp_path: Path) -> None:
    source = _dat(tmp_path / "volume.dat", size=100)
    candidate = _idx(tmp_path / "volume.idx", (7, 8, 1), (99, 96, 9))

    result = inspect_index_bounds(candidate, source)

    assert result["valid"] is False
    assert result["maximum_needle_end"] == 136
    assert result["violations"] == [{
        "needle_id": 99,
        "offset": 96,
        "size": 9,
        "end": 136,
        "bytes_past_eof": 36,
    }]


def test_index_bounds_rejects_partial_index_record(tmp_path: Path) -> None:
    source = _dat(tmp_path / "volume.dat")
    candidate = tmp_path / "volume.idx"
    candidate.write_bytes(b"partial")

    with pytest.raises(RepairHelperError, match="not a multiple"):
        inspect_index_bounds(candidate, source)


def test_index_bounds_ignores_tombstones_and_remote_markers(tmp_path: Path) -> None:
    source = _dat(tmp_path / "volume.dat", size=32)
    candidate = _idx(tmp_path / "volume.idx", (1, 0, 1000), (2, 24, -1))

    result = inspect_index_bounds(candidate, source)

    assert result["valid"] is True
    assert result["maximum_needle_end"] == 0


def test_recover_incomplete_tail_preserves_bytes_before_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _dat(tmp_path / "photos_1.dat", size=100)
    live_index = _idx(tmp_path / "photos_1.live.idx", (7, 8, 1))
    backup = tmp_path / "backups" / "photos_1.incomplete-tail"
    expected = fingerprint(source)
    monkeypatch.setattr(repair_helper, "build_candidate", lambda _args: {
        "success": False,
        "manual_intervention_required": True,
        "candidate_bounds": {
            "source_size": 100,
            "maximum_entry_offset": 96,
            "maximum_valid_needle_end": 48,
            "violations": [{
                "needle_id": 99, "offset": 96, "size": 9,
                "end": 136, "bytes_past_eof": 36,
            }],
        },
    })
    args = SimpleNamespace(
        source_dat=str(source), live_index=str(live_index), tail_backup=str(backup),
        expected_fingerprint=json.dumps(expected), maximum_tail_bytes=16,
    )

    result = repair_helper.recover_incomplete_tail(args)

    assert result["success"] is True
    assert source.stat().st_size == 96
    assert backup.read_bytes() == bytes(4)
    assert result["source_fingerprint_after"] == fingerprint(source)
