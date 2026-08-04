from __future__ import annotations

import pytest

from s3_storage_node.generation import GenerationError, ensure_ip_forwarding


class FakeSysctl:
    def __init__(self, value: str, *, read_error: OSError | None = None, write_error: OSError | None = None) -> None:
        self.value = value
        self.read_error = read_error
        self.write_error = write_error
        self.writes: list[str] = []

    def read_text(self, *, encoding: str) -> str:
        assert encoding == "ascii"
        if self.read_error is not None:
            raise self.read_error
        return self.value

    def write_text(self, value: str, *, encoding: str) -> None:
        assert encoding == "ascii"
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(value)
        self.value = value


def test_pre_enabled_read_only_forwarding_needs_no_write() -> None:
    sysctl = FakeSysctl("1\n", write_error=OSError(30, "read-only file system"))

    ensure_ip_forwarding(sysctl)  # type: ignore[arg-type]

    assert sysctl.writes == []


def test_disabled_forwarding_is_enabled_and_verified() -> None:
    sysctl = FakeSysctl("0\n")

    ensure_ip_forwarding(sysctl)  # type: ignore[arg-type]

    assert sysctl.writes == ["1\n"]
    assert sysctl.value == "1\n"


def test_unreadable_forwarding_state_fails_closed() -> None:
    sysctl = FakeSysctl("", read_error=OSError(13, "permission denied"))

    with pytest.raises(GenerationError, match="unable to inspect namespace forwarding"):
        ensure_ip_forwarding(sysctl)  # type: ignore[arg-type]


def test_disabled_read_only_forwarding_fails_closed() -> None:
    sysctl = FakeSysctl("0\n", write_error=OSError(30, "read-only file system"))

    with pytest.raises(GenerationError, match="unable to enable namespace forwarding"):
        ensure_ip_forwarding(sysctl)  # type: ignore[arg-type]
