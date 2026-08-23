from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest

from s3_storage_node.s3check import _request


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TRANSPORT_CHAOS") != "1",
    reason="set RUN_TRANSPORT_CHAOS=1 for the Docker CIFS/SSHFS chaos harness",
)


SAMBA_IP = "172.31.250.10"
SSHD_IP = "172.31.250.11"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _compose(
    project: str,
    env: dict[str, str],
    *arguments: str,
    check: bool = True,
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            "tests/chaos/docker-compose.yml",
            *arguments,
        ],
        env=env,
        check=check,
        timeout=timeout,
    )


def _health(port: int) -> dict[str, object] | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        return json.loads(exc.read())
    except (OSError, URLError, json.JSONDecodeError):
        return None


def _wait(
    predicate: Callable[[], object],
    description: str,
    *,
    timeout: float = 180,
    interval: float = 0.25,
):
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except Exception as exc:  # noqa: BLE001 - polling boundary
            last = exc
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {description}; last result: {last!r}")


def _wait_ready(port: int, transport: str, *, timeout: float = 240) -> dict[str, object]:
    def ready():
        snapshot = _health(port)
        if not snapshot or not snapshot.get("ready"):
            return None
        storage = snapshot.get("storage", {})
        if not isinstance(storage, dict):
            return None
        details = storage.get("transport:data", {})
        if not isinstance(details, dict) or details.get("active_transport") != transport:
            return None
        volumes = snapshot.get("seaweed_volumes", {})
        if not isinstance(volumes, dict) or not volumes.get("checked"):
            return None
        if volumes.get("unexpected_readonly") != 0:
            return None
        history = snapshot.get("generation_history", {})
        if not isinstance(history, dict) or not history.get("indexes_certified"):
            return None
        return snapshot

    return _wait(ready, f"ready state on {transport}", timeout=timeout)


def _transport_status(project: str, env: dict[str, str]) -> dict[str, object] | None:
    result = _compose(
        project,
        env,
        "exec",
        "-T",
        "node",
        "s3-storage-node",
        "transport-status",
        "--config",
        "/etc/s3-storage-node/config.toml",
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return None


def _wait_primary_failure(project: str, env: dict[str, str]) -> dict[str, object]:
    def failed():
        status = _transport_status(project, env)
        if not status:
            return None
        failures = status.get("failed", {})
        if isinstance(failures, dict) and "cifs-primary" in failures:
            return status
        return None

    return _wait(failed, "CIFS failure persistence", timeout=180, interval=0.15)


def _drop_smb(project: str, env: dict[str, str]) -> None:
    _compose(
        project,
        env,
        "exec",
        "-T",
        "samba",
        "iptables",
        "-I",
        "INPUT",
        "1",
        "-p",
        "tcp",
        "--dport",
        "445",
        "-j",
        "DROP",
    )


def _restore_smb(project: str, env: dict[str, str]) -> None:
    _compose(
        project,
        env,
        "exec",
        "-T",
        "samba",
        "iptables",
        "-D",
        "INPUT",
        "-p",
        "tcp",
        "--dport",
        "445",
        "-j",
        "DROP",
        check=False,
    )


def _s3(port: int, method: str, path: str, body: bytes, access: str, secret: str) -> tuple[int, bytes]:
    return _request(
        "127.0.0.1",
        port,
        method,
        path,
        body,
        access,
        secret,
        f"127.0.0.1:{port}",
    )


def _create_bucket(port: int, bucket: str, access: str, secret: str) -> None:
    status, body = _s3(port, "PUT", f"/{bucket}", b"", access, secret)
    assert status in {200, 204, 409}, (status, body[:500])


def _put_object(port: int, bucket: str, key: str, body: bytes, access: str, secret: str) -> None:
    status, response = _s3(port, "PUT", f"/{bucket}/{key}", body, access, secret)
    assert status in {200, 201, 204}, (status, response[:500])


def _get_object(port: int, bucket: str, key: str, access: str, secret: str) -> bytes:
    status, body = _s3(port, "GET", f"/{bucket}/{key}", b"", access, secret)
    assert status == 200, (status, body[:500])
    return body


def _write_config(path: Path) -> None:
    path.write_text(
        f"""
[appliance]
name = "transport-chaos"
state_dir = "/var/lib/s3-storage-node"
runtime_dir = "/run/s3-storage-node"
uid = 10001
gid = 10001
health_host = "0.0.0.0"
health_port = 9090
probe_interval_seconds = 1
full_probe_interval_seconds = 2
probe_timeout_seconds = 3
startup_timeout_seconds = 30
shutdown_grace_seconds = 4
recovery_initial_seconds = 4
recovery_max_seconds = 4
recovery_stability_seconds = 2
recovery_probe_interval_seconds = 1
recovery_successes_required = 2
worker_fencing_mode = "namespace"
worker_host_address = "169.254.254.1/30"
worker_address = "169.254.254.2/30"
worker_gateway = "169.254.254.1"
s3_canary_enabled = true

[storage.data]
type = "cifs"
source = "//{SAMBA_IP}/share"
mountpoint = "/run/s3-storage-node/mounts/data"
subdirectory = "seaweedfs"
credentials_file = "/run/secrets/cifs_credentials"
sentinel_id = "chaos-dataset-v2"
allow_initialize = true
min_free_bytes = 0
io_failure_policy = "soft"
minimum_smb_dialect = "3.0"
handle_reconnect_policy = "disabled"
multichannel_policy = "disabled"
require_transport_observability = false
mount_options = [
  "vers=3.1.1",
  "uid=10001",
  "gid=10001",
  "file_mode=0660",
  "dir_mode=0770",
  "cache=none",
  "actimeo=0",
  "echo_interval=1",
  "nosharesock"
]

[storage.data.failover]
enabled = true
primary_name = "cifs-primary"
primary_priority = 10
failback_policy = "manual"
failure_cooldown_seconds = 300

[[storage.data.failover.transports]]
name = "sshfs-secondary"
type = "sshfs"
priority = 20
source = "root@{SSHD_IP}:/srv/storage"
identity_file = "/run/secrets/ssh_identity"
known_hosts_file = "/run/secrets/ssh_known_hosts"
port = 22
mount_options = []

[storage.metadata]
type = "path"
mountpoint = "/var/lib/s3-storage-node/metadata"
subdirectory = ""
sentinel_id = "chaos-metadata"
allow_initialize = true
min_free_bytes = 0

[storage.index]
type = "path"
mountpoint = "/var/lib/s3-storage-node/index"
subdirectory = ""
sentinel_id = "chaos-index"
allow_initialize = true
min_free_bytes = 0

[metadata]
backend = "embedded"
target = "metadata"
directory = "filer"

[index]
target = "index"
directory = "volume-indexes"

[seaweed]
volume_directory = "volumes"
auto_index_repair_enabled = true
index_repair_concurrency = 1
index_repair_timeout_seconds = 120
master_port = 9333
volume_port = 8080
filer_port = 8888
s3_internal_port = 18333
volume_max = 2
volume_size_limit_mb = 64
default_replication = "000"
filer_max_mb = 1
data_center = ""
rack = ""
disk_type = ""
encrypt_volume_data = false
master_extra_args = []
volume_extra_args = []
filer_extra_args = []
s3_extra_args = []

[s3]
host = "0.0.0.0"
port = 8333
domain_name = ""
allowed_origins = "*"
external_url = ""
auth_mode = "static"
access_key_file = "/run/secrets/s3_access_key"
secret_key_file = "/run/secrets/s3_secret_key"
auth_config_file = ""
canary_access_key_file = ""
canary_secret_key_file = ""
iam_config_file = ""
audit_log_config_file = ""
tls_mode = "off"
tls_pem_file = ""
""".lstrip(),
        encoding="utf-8",
    )


def _prepare_lab(tmp_path: Path) -> tuple[dict[str, str], str, int, int, str, str]:
    tools = ("docker", "ssh-keygen")
    for tool in tools:
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    if not Path("/dev/fuse").exists():
        pytest.skip("/dev/fuse is not available")
    compose = _run(["docker", "compose", "version"], check=False)
    if compose.returncode != 0:
        pytest.skip("docker compose is not available")

    lab = tmp_path.resolve()
    (lab / "state").mkdir()
    _write_config(lab / "config.toml")
    access = "chaos-access"
    secret = "chaos-secret-" + secrets.token_hex(16)
    (lab / "cifs_credentials").write_text(
        "username=seaweed\npassword=chaos-password\ndomain=\n",
        encoding="utf-8",
    )
    (lab / "s3_access_key").write_text(access + "\n", encoding="utf-8")
    (lab / "s3_secret_key").write_text(secret + "\n", encoding="utf-8")
    _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(lab / "ssh_identity")])
    _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(lab / "ssh_host_ed25519_key")])
    (lab / "authorized_keys").write_text(
        (lab / "ssh_identity.pub").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    host_public = (lab / "ssh_host_ed25519_key.pub").read_text(encoding="utf-8").split()
    (lab / "ssh_known_hosts").write_text(
        f"{SSHD_IP} {host_public[0]} {host_public[1]}\n",
        encoding="utf-8",
    )
    for name in (
        "cifs_credentials",
        "s3_access_key",
        "s3_secret_key",
        "ssh_identity",
        "ssh_host_ed25519_key",
        "authorized_keys",
        "ssh_known_hosts",
    ):
        (lab / name).chmod(0o600)

    s3_port = _free_port()
    health_port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "CHAOS_DIR": str(lab),
            "CHAOS_S3_PORT": str(s3_port),
            "CHAOS_HEALTH_PORT": str(health_port),
            "COMPOSE_PROGRESS": "plain",
        }
    )
    project = "s3chaos" + uuid.uuid4().hex[:10]
    return env, project, s3_port, health_port, access, secret


def _assert_objects(
    port: int,
    bucket: str,
    objects: dict[str, bytes],
    access: str,
    secret: str,
) -> None:
    for key, expected in objects.items():
        assert _get_object(port, bucket, key, access, secret) == expected


def _node_logs(project: str, env: dict[str, str]) -> str:
    return _compose(project, env, "logs", "--no-color", "node", check=False, timeout=30).stdout


def test_real_cifs_to_sshfs_chaos_failover(tmp_path: Path) -> None:
    env, project, s3_port, health_port, access, secret = _prepare_lab(tmp_path)
    started = False
    try:
        _compose(project, env, "build", timeout=900)
        _compose(project, env, "up", "-d", timeout=120)
        started = True

        primary = _wait_ready(health_port, "cifs-primary")
        primary_generation = int(primary["generation"]["id"])
        data_health = primary["storage"]["data"]
        assert data_health["sentinel_version"] == 2
        assert data_health["dataset_id"] == "chaos-dataset-v2"

        bucket = "chaos-" + uuid.uuid4().hex[:16]
        _create_bucket(s3_port, bucket, access, secret)
        objects = {
            f"before-{index}": secrets.token_bytes(2 * 1024 * 1024)
            for index in range(3)
        }
        for key, body in objects.items():
            _put_object(s3_port, bucket, key, body, access, secret)
        _assert_objects(s3_port, bucket, objects, access, secret)

        # Hard-drop SMB packets while the real appliance is online. The helper
        # timeout must become a transport failure, readiness must be withdrawn,
        # and the generation veth must be absent before SSHFS recovery starts.
        _drop_smb(project, env)
        unavailable = _wait(
            lambda: (
                (snapshot := _health(health_port))
                and not snapshot.get("ready")
                and int(snapshot["generation"]["id"]) == primary_generation
                and snapshot
            ),
            "readiness withdrawal after CIFS loss",
            timeout=60,
        )
        assert int(unavailable["generation"]["id"]) == primary_generation
        _wait_primary_failure(project, env)
        link = _compose(
            project,
            env,
            "exec",
            "-T",
            "node",
            "ip",
            "link",
            "show",
            "s3g-host",
            check=False,
        )
        assert link.returncode != 0, "old generation network link survived the recorded CIFS failure"

        secondary = _wait_ready(health_port, "sshfs-secondary")
        secondary_generation = int(secondary["generation"]["id"])
        assert secondary_generation > primary_generation
        assert secondary["storage"]["data"]["sentinel_version"] == 2
        assert secondary["generation_history"]["counters"]["cause:storage_failure"] >= 1
        assert secondary["seaweed_volumes"]["orphan_deletion_safe"] is True
        _assert_objects(s3_port, bucket, objects, access, secret)
        after_key = "after-sshfs"
        objects[after_key] = secrets.token_bytes(2 * 1024 * 1024)
        _put_object(s3_port, bucket, after_key, objects[after_key], access, secret)
        _assert_objects(s3_port, bucket, objects, access, secret)

        logs = _node_logs(project, env)
        fence_marker = f'"event":"worker_generation_fenced","generation":{primary_generation}'
        secondary_marker = '"event":"storage_transport_selected"'
        assert fence_marker in logs
        assert '"transport":"sshfs-secondary"' in logs
        assert logs.index(fence_marker) < logs.rindex(secondary_marker)

        # Restoring SMB must not trigger automatic failback.
        _restore_smb(project, env)
        time.sleep(5)
        still_secondary = _health(health_port)
        assert still_secondary and still_secondary["ready"] is True
        assert still_secondary["storage"]["transport:data"]["active_transport"] == "sshfs-secondary"

        # Controlled failback must use another fenced generation and retain all
        # objects written before and after the transport switch.
        _compose(
            project,
            env,
            "exec",
            "-T",
            "node",
            "s3-storage-node",
            "transport-select",
            "--config",
            "/etc/s3-storage-node/config.toml",
            "--transport",
            "cifs-primary",
        )
        primary_again = _wait_ready(health_port, "cifs-primary")
        primary_again_generation = int(primary_again["generation"]["id"])
        assert primary_again_generation > secondary_generation
        _assert_objects(s3_port, bucket, objects, access, secret)

        # Repeat the failure, but crash the guardian after it persisted the CIFS
        # failure and fenced the worker. On restart the persistent selector must
        # continue with SSHFS rather than forgetting the half-completed failover.
        _drop_smb(project, env)
        _wait_primary_failure(project, env)
        _compose(project, env, "kill", "-s", "KILL", "node", timeout=30)

        # Model the ordering divergence a hard fence can leave behind: the
        # authoritative remote .dat survives, while the local .idx contains a
        # final entry whose referenced offset is far beyond EOF. The container
        # is stopped here, so no SeaweedFS writer can race this fault injection.
        # Use a one-shot container with the same state mount because its
        # root-owned contents are not readable by the GitHub runner account.
        # Its entrypoint is only this bounded mutation; it starts no guardian or
        # SeaweedFS component and exits before recovery begins.
        node_image = _compose(project, env, "images", "-q", "node").stdout.strip().splitlines()[-1]
        injection = _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "-v",
                f"{env['CHAOS_DIR']}/state:/var/lib/s3-storage-node",
                "--entrypoint",
                "python3",
                node_image,
                "-c",
                (
                    "import json, os, pathlib, struct, sys; "
                    "root=pathlib.Path(sys.argv[1]); bucket=sys.argv[2]; "
                    "indexes=sorted(root.glob(f'{bucket}_*.idx')); "
                    "assert indexes, f'no workload index under {root}'; "
                    "path=indexes[0]; "
                    "handle=path.open('ab'); "
                    "handle.write(struct.pack('>QII', 0xffffffffffffffff, 0x3fffffff, 100)); "
                    "handle.flush(); os.fsync(handle.fileno()); handle.close(); "
                    "print(json.dumps({'name': path.name, "
                    "'volume_id': int(path.stem.rsplit('_', 1)[-1])}))"
                ),
                "/var/lib/s3-storage-node/index/volume-indexes",
                bucket,
            ],
            timeout=60,
        )
        injected = json.loads(injection.stdout.strip().splitlines()[-1])
        damaged_index_name = str(injected["name"])
        damaged_volume_id = int(injected["volume_id"])

        # The four-second deadline above deliberately exercises hard fencing
        # during transport loss.  Give the subsequent repair generation a
        # still-bounded deadline long enough for SeaweedFS's own shutdown path
        # (the volume server alone can wait roughly ten seconds) so the test
        # reaches the offline-only repair rather than repeatedly hard-fencing.
        config_path = Path(env["CHAOS_DIR"]) / "config.toml"
        config_text = config_path.read_text(encoding="utf-8")
        assert "shutdown_grace_seconds = 4" in config_text
        config_path.write_text(
            config_text.replace("shutdown_grace_seconds = 4", "shutdown_grace_seconds = 25", 1),
            encoding="utf-8",
        )

        _compose(project, env, "up", "-d", "node", timeout=60)
        restarted_secondary = _wait_ready(health_port, "sshfs-secondary", timeout=300)
        assert int(restarted_secondary["generation"]["id"]) > primary_again_generation
        assert restarted_secondary["generation_history"]["counters"]["cause:guardian_restart_detected"] >= 1
        repair = restarted_secondary["index_repair"]
        assert repair["counters"]["succeeded_total"] >= 1
        assert damaged_volume_id in repair["verified_volume_ids"]
        backup_check = _compose(
            project,
            env,
            "exec",
            "-T",
            "node",
            "python3",
            "-c",
            (
                "import pathlib, sys; "
                "root=pathlib.Path(sys.argv[1]); name=sys.argv[2]; "
                "matches=list(root.glob(f'*/{name}')); "
                "raise SystemExit(0 if matches else 1)"
            ),
            "/var/lib/s3-storage-node/index/volume-indexes/.s3-storage-node-repair/backups",
            damaged_index_name,
            check=False,
            timeout=30,
        )
        assert backup_check.returncode == 0, "automatic repair did not retain the old index backup"
        _assert_objects(s3_port, bucket, objects, access, secret)
        restart_key = "after-guardian-restart"
        objects[restart_key] = secrets.token_bytes(2 * 1024 * 1024)
        _put_object(s3_port, bucket, restart_key, objects[restart_key], access, secret)
        _assert_objects(s3_port, bucket, objects, access, secret)

        _restore_smb(project, env)
        time.sleep(5)
        assert _wait_ready(health_port, "sshfs-secondary")["ready"] is True

        # Inspect the shared backing directory through the independent SSH
        # server: the first enrollment must have created the strict V2 schema.
        sentinel_result = _compose(
            project,
            env,
            "exec",
            "-T",
            "sshd",
            "cat",
            "/srv/storage/seaweedfs/.s3-storage-node.json",
        )
        sentinel = json.loads(sentinel_result.stdout)
        assert sentinel == {
            "schema": "s3-storage-node/dataset-sentinel",
            "version": 2,
            "sentinel_id": "chaos-dataset-v2",
            "dataset_id": "chaos-dataset-v2",
            "target": "data",
            "subdirectory": "seaweedfs",
            "transport_independent": True,
        }

        # Downgrade only the sentinel document to the legacy V1 shape and prove
        # a full guardian restart still mounts, certifies, and serves the same
        # objects without rewriting the operator's existing sentinel.
        _compose(project, env, "stop", "node", timeout=60)
        legacy = json.dumps(
            {"version": 1, "sentinel_id": "chaos-dataset-v2", "target": "data"},
            sort_keys=True,
        )
        _compose(
            project,
            env,
            "exec",
            "-T",
            "sshd",
            "sh",
            "-c",
            f"printf '%s\\n' '{legacy}' > /srv/storage/seaweedfs/.s3-storage-node.json",
        )
        _compose(project, env, "start", "node", timeout=60)
        legacy_ready = _wait_ready(health_port, "sshfs-secondary", timeout=300)
        assert legacy_ready["storage"]["data"]["sentinel_version"] == 1
        assert legacy_ready["storage"]["data"]["sentinel_schema"] == "legacy-v1"
        _assert_objects(s3_port, bucket, objects, access, secret)
        unchanged = _compose(
            project,
            env,
            "exec",
            "-T",
            "sshd",
            "cat",
            "/srv/storage/seaweedfs/.s3-storage-node.json",
        )
        assert json.loads(unchanged.stdout) == json.loads(legacy)
    except Exception as exc:
        logs = _node_logs(project, env) if started else "node was not started"
        raise AssertionError(f"transport chaos harness failed: {exc}\n\nnode logs:\n{logs}") from exc
    finally:
        if started:
            _restore_smb(project, env)
        _compose(
            project,
            env,
            "down",
            "-v",
            "--remove-orphans",
            check=False,
            timeout=120,
        )
