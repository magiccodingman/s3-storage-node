from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from s3_storage_node.generation import WorkerGeneration


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_NAMESPACE_TESTS") != "1"
    or os.geteuid() != 0
    or any(shutil.which(binary) is None for binary in ("unshare", "nsenter", "ip", "iptables", "mount", "umount")),
    reason="requires root namespace tooling and RUN_NAMESPACE_TESTS=1",
)


def test_worker_mount_and_network_namespaces_can_be_fenced(tmp_path: Path) -> None:
    mountpoint = tmp_path / "isolated"
    mountpoint.mkdir()
    generation = WorkerGeneration(
        generation=1,
        token="integration",
        mode="namespace",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        host_address="169.254.254.1/30",
        worker_address="169.254.254.2/30",
        gateway="169.254.254.1",
    )
    generation.start()
    try:
        subprocess.run(
            generation.enter_command(["mount", "-t", "tmpfs", "tmpfs", str(mountpoint)]),
            check=True,
        )
        subprocess.run(generation.enter_command(["touch", str(mountpoint / "inside")]), check=True)
        assert not (mountpoint / "inside").exists()
        assert subprocess.run(["ip", "link", "show", generation.HOST_LINK], capture_output=True).returncode == 0
        generation.fence("test fence")
        assert subprocess.run(["ip", "link", "show", generation.HOST_LINK], capture_output=True).returncode != 0
    finally:
        try:
            subprocess.run(generation.enter_command(["umount", "-l", str(mountpoint)]), check=False)
        except Exception:
            pass
        generation.retire()
