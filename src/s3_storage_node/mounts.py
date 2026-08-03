from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class MountInfo:
    mountpoint: str
    filesystem: str
    source: str
    mount_options: frozenset[str] = frozenset()
    super_options: frozenset[str] = frozenset()


def decode_mountinfo_path(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _split_options(value: str) -> frozenset[str]:
    return frozenset(option.strip() for option in value.split(",") if option.strip())


def read_mountinfo(path: str = "/proc/self/mountinfo") -> list[MountInfo]:
    entries: list[MountInfo] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            before, separator, after = line.rstrip("\n").partition(" - ")
            if not separator:
                continue
            left = before.split()
            right = after.split()
            if len(left) < 6 or len(right) < 2:
                continue
            entries.append(MountInfo(
                mountpoint=decode_mountinfo_path(left[4]),
                filesystem=right[0],
                source=decode_mountinfo_path(right[1]),
                mount_options=_split_options(left[5]),
                super_options=_split_options(right[2] if len(right) > 2 else ""),
            ))
    return entries


def find_mount(path: Path, entries: Iterable[MountInfo] | None = None) -> MountInfo | None:
    wanted = os.path.realpath(path)
    for entry in entries or read_mountinfo():
        if os.path.realpath(entry.mountpoint) == wanted:
            return entry
    return None
