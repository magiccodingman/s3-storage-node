from __future__ import annotations

import struct
from pathlib import Path

import pytest

from s3_storage_node.index_repair_helper import RepairHelperError, inspect_index_bounds


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
