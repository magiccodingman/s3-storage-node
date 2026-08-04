from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from s3_storage_node.config_types import TargetConfig
from s3_storage_node.storage import mount_target, probe_target, unmount_target, verify_or_initialize_sentinel


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SSHFS_INTEGRATION") != "1",
    reason="set RUN_SSHFS_INTEGRATION=1 for the privileged SSHFS integration test",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = b"" if process.stderr is None else process.stderr.read()
            raise RuntimeError(f"test sshd exited before accepting connections: {stderr.decode(errors='replace')}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("test sshd did not become ready")


def _start_sshd(tmp_path: Path, tools: dict[str, str], port: int, password: str):
    host_key = tmp_path / "host"
    client_key = tmp_path / "client"
    subprocess.run([tools["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)], check=True)
    subprocess.run([tools["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True)
    authorized = tmp_path / "authorized_keys"
    authorized.write_text(client_key.with_suffix(".pub").read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["chpasswd"], input=f"root:{password}\n", text=True, check=True)
    config = tmp_path / "sshd_config"
    config.write_text("\n".join([
        f"Port {port}", "ListenAddress 127.0.0.1", f"HostKey {host_key}",
        f"PidFile {tmp_path / 'sshd.pid'}", f"AuthorizedKeysFile {authorized}",
        "PermitRootLogin yes", "PasswordAuthentication yes", "KbdInteractiveAuthentication no",
        "PubkeyAuthentication yes", "StrictModes no", "UsePAM no", "Subsystem sftp internal-sftp",
    ]) + "\n", encoding="utf-8")
    Path("/run/sshd").mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [tools["sshd"], "-D", "-e", "-f", str(config)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    known_hosts = tmp_path / "known_hosts"
    host_public = host_key.with_suffix(".pub").read_text(encoding="utf-8").split()
    known_hosts.write_text(f"[127.0.0.1]:{port} {host_public[0]} {host_public[1]}\n", encoding="utf-8")
    return process, client_key, known_hosts


@pytest.mark.parametrize("auth_mode", ["key", "password"])
def test_real_sshfs_mount_and_durability_probe(tmp_path: Path, auth_mode: str) -> None:
    if not Path("/dev/fuse").exists():
        pytest.skip("/dev/fuse is not available on this runner")
    tools = {command: shutil.which(command) for command in ("sshfs", "sshd", "ssh-keygen")}
    for command, executable in tools.items():
        if executable is None:
            pytest.skip(f"{command} is not installed")
    remote = tmp_path / "remote"
    mountpoint = tmp_path / "mount"
    remote.mkdir()
    mountpoint.mkdir()
    password = "integration-password"
    port = _free_port()
    sshd, client_key, known_hosts = _start_sshd(tmp_path, tools, port, password)
    runtime_key = tmp_path / "runtime-key"
    credentials = tmp_path / "credentials"
    credentials.write_text(f"username=root\npassword={password}\ndomain=\n")
    common_options = (
        f"UserKnownHostsFile={known_hosts}", "StrictHostKeyChecking=yes", f"port={port}", "reconnect",
        "ServerAliveInterval=2", "ServerAliveCountMax=2", "sshfs_sync", "dir_cache=no", "allow_other",
        "default_permissions", f"uid={os.getuid()}", f"gid={os.getgid()}", "umask=007",
    )
    if auth_mode == "key":
        auth_options = (
            f"IdentityFile={runtime_key}", "BatchMode=yes", "PreferredAuthentications=publickey",
            "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
        )
    else:
        auth_options = (
            "password_stdin", "BatchMode=no", "PreferredAuthentications=password", "PubkeyAuthentication=no",
            "KbdInteractiveAuthentication=no", "NumberOfPasswordPrompts=1",
        )
    target = TargetConfig(
        name="data", type="sshfs", mountpoint=mountpoint, subdirectory="seaweedfs",
        sentinel_id=f"integration-{auth_mode}", allow_initialize=True, min_free_bytes=0,
        source=f"root@127.0.0.1:{remote}", transport_name=f"sshfs-{auth_mode}", ssh_auth_mode=auth_mode,
        ssh_identity_file=str(client_key) if auth_mode == "key" else "",
        ssh_credentials_file=str(credentials) if auth_mode == "password" else "",
        ssh_known_hosts_file=str(known_hosts),
        ssh_runtime_identity_file=str(runtime_key) if auth_mode == "key" else "",
        ssh_runtime_pid_file=str(tmp_path / "sshfs.pid"), ssh_port=port,
        mount_options=(*auth_options, *common_options),
    )
    try:
        _wait_port(port, sshd)
        mount_target(target)
        verify_or_initialize_sentinel(target, os.getuid(), os.getgid())
        result = probe_target(target, full=True)
        assert result["transport_type"] == "sshfs"
        assert result["transport_name"] == f"sshfs-{auth_mode}"
        assert result["ssh_auth_mode"] == auth_mode
        assert result["durability_probe_bytes"] > 0
    finally:
        try:
            unmount_target(target)
        finally:
            sshd.terminate()
            try:
                sshd.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sshd.kill()
