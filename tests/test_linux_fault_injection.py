from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PRIVILEGED_TESTS") != "1" or os.geteuid() != 0,
    reason="requires RUN_PRIVILEGED_TESTS=1 and root",
)


def run_as_nobody(command: list[str]) -> subprocess.CompletedProcess[str]:
    nobody = pwd.getpwnam("nobody")

    def demote() -> None:
        os.setgroups([])
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)

    return subprocess.run(command, text=True, capture_output=True, preexec_fn=demote, check=False)


def test_mode_zero_barrier_rejects_unprivileged_local_fallback(tmp_path: Path) -> None:
    barrier = tmp_path / "data"
    barrier.mkdir()
    barrier.chmod(0)

    result = run_as_nobody(["sh", "-c", f"echo forbidden > {barrier / 'volume.dat'}"])

    assert result.returncode != 0
    assert not (barrier / "volume.dat").exists()


def test_real_mount_overlays_barrier_then_fails_closed_after_unmount(tmp_path: Path) -> None:
    required = ("mount", "umount", "mkfs.ext4")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        pytest.skip(f"missing tools: {', '.join(missing)}")

    image = tmp_path / "disk.img"
    mountpoint = tmp_path / "managed"
    image.write_bytes(b"\0" * (64 * 1024 * 1024))
    mountpoint.mkdir()
    mountpoint.chmod(0)

    subprocess.run(["mkfs.ext4", "-F", str(image)], check=True, capture_output=True)
    subprocess.run(["mount", "-o", "loop", str(image), str(mountpoint)], check=True)
    try:
        mountpoint.chmod(0o777)
        writable = run_as_nobody(["sh", "-c", f"echo remote > {mountpoint / 'remote.dat'}"])
        assert writable.returncode == 0, writable.stderr
        assert (mountpoint / "remote.dat").read_text(encoding="utf-8").strip() == "remote"
    finally:
        subprocess.run(["umount", str(mountpoint)], check=True)

    assert (mountpoint.stat().st_mode & 0o777) == 0
    fallback = run_as_nobody(["sh", "-c", f"echo forbidden > {mountpoint / 'local.dat'}"])
    assert fallback.returncode != 0
    assert not (mountpoint / "local.dat").exists()


def test_lazy_detach_also_reveals_unwritable_barrier(tmp_path: Path) -> None:
    image = tmp_path / "disk.img"
    mountpoint = tmp_path / "managed"
    image.write_bytes(b"\0" * (64 * 1024 * 1024))
    mountpoint.mkdir()
    mountpoint.chmod(0)

    subprocess.run(["mkfs.ext4", "-F", str(image)], check=True, capture_output=True)
    subprocess.run(["mount", "-o", "loop", str(image), str(mountpoint)], check=True)
    mountpoint.chmod(0o777)
    subprocess.run(["umount", "-l", str(mountpoint)], check=True)

    fallback = run_as_nobody(["sh", "-c", f"touch {mountpoint / 'should-not-exist'}"])
    assert fallback.returncode != 0
    assert not (mountpoint / "should-not-exist").exists()
