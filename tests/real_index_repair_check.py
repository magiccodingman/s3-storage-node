"""Privileged real-binary check run by the Docker CI job, not pytest collection."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path


UID = 10001
GID = 10001
MASTER_PORT = 19333
VOLUME_PORT = 18080


def wait_json(url: str, predicate, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=1) as response:
                payload = json.load(response)
            if predicate(payload):
                return payload
        except Exception as exc:  # noqa: BLE001 - bounded integration observer
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {url}: {last_error}")


def upload(master_port: int, payload: bytes) -> None:
    assignment = wait_json(
        f"http://127.0.0.1:{master_port}/dir/assign",
        lambda item: bool(item.get("fid")),
    )
    boundary = "----s3-index-repair-" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="payload"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    connection = http.client.HTTPConnection("127.0.0.1", VOLUME_PORT, timeout=5)
    connection.request(
        "POST", f"/{assignment['fid']}", body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
    )
    response = connection.getresponse()
    response.read()
    connection.close()
    if response.status != 201:
        raise AssertionError(f"SeaweedFS upload failed with HTTP {response.status}")


def start_process(command: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        user=UID,
        group=GID,
        extra_groups=[],
    )


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_helper(weed: Path, source: Path, staging: Path, base_name: str, volume_id: int) -> tuple[int, dict]:
    command = [
        "unshare", "--mount", "--propagation", "private", "--",
        sys.executable, "-m", "s3_storage_node.index_repair_helper",
        "--source-dat", str(source),
        "--staging-dir", str(staging),
        "--base-name", base_name,
        "--volume-id", str(volume_id),
        "--weed-binary", str(weed),
        "--uid", str(UID),
        "--gid", str(GID),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"repair helper produced no result: {result.stderr}")
    return result.returncode, json.loads(lines[-1])


def main() -> int:
    weed = Path(os.environ.get("WEED_BINARY", "/usr/local/bin/weed"))
    if not weed.is_file():
        raise AssertionError(f"bundled weed binary is missing: {weed}")
    with tempfile.TemporaryDirectory(prefix="s3-real-index-repair-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o755)
        master_dir = root / "master"
        data_dir = root / "data"
        index_dir = root / "index"
        for directory in (master_dir, data_dir, index_dir):
            directory.mkdir()
            os.chown(directory, UID, GID)
            os.chmod(directory, 0o750)

        master = start_process([
            str(weed), "master", "-ip=127.0.0.1", "-ip.bind=127.0.0.1",
            f"-port={MASTER_PORT}", f"-mdir={master_dir}",
        ])
        volume: subprocess.Popen[bytes] | None = None
        volume_command = [
            str(weed), "volume", "-ip=127.0.0.1", "-ip.bind=127.0.0.1",
            f"-port={VOLUME_PORT}", f"-master=127.0.0.1:{MASTER_PORT}",
            f"-dir={data_dir}", f"-dir.idx={index_dir}", "-max=1", "-index=memory",
        ]
        try:
            volume = start_process(volume_command)
            wait_json(
                f"http://127.0.0.1:{MASTER_PORT}/dir/assign",
                lambda item: bool(item.get("fid")),
            )
            upload(MASTER_PORT, b"first complete record")
            upload(MASTER_PORT, b"second complete record")
            stop_process(volume)
            volume = None

            dat = data_dir / "1.dat"
            idx = index_dir / "1.idx"
            original_dat_hash = hashlib.sha256(dat.read_bytes()).hexdigest()
            complete_idx = idx.read_bytes()
            if len(complete_idx) != 32:
                raise AssertionError(f"expected two real index entries, got {len(complete_idx)} bytes")

            # Complete .dat records beyond the old .idx must be recovered.
            idx.write_bytes(complete_idx[:16])
            os.chown(idx, UID, GID)
            code, rebuilt = run_helper(weed, dat, root / "stage-ahead", "1", 1)
            if code != 0 or not rebuilt.get("success"):
                raise AssertionError(f"complete-tail reconstruction failed: {rebuilt}")
            candidate = Path(rebuilt["candidate"])
            if candidate.read_bytes() != complete_idx:
                raise AssertionError("reconstructed index did not include the complete .dat tail")
            if not rebuilt.get("readonly_mount_verified") or not rebuilt.get("write_rejected"):
                raise AssertionError("repair helper did not prove read-only .dat exposure")

            # A real index entry beyond EOF must make upstream report ReadOnly.
            impossible_entry = struct.pack(">QII", 0xFFFFFFFFFFFFFFFF, 0x3FFFFFFF, 100)
            idx.write_bytes(complete_idx + impossible_entry)
            os.chown(idx, UID, GID)
            volume = start_process(volume_command)
            damaged_status = wait_json(
                f"http://127.0.0.1:{VOLUME_PORT}/status",
                lambda item: any(volume.get("Id") == 1 for volume in item.get("Volumes") or []),
            )
            volume_one = next(item for item in damaged_status["Volumes"] if item["Id"] == 1)
            if volume_one.get("ReadOnly") is not True:
                raise AssertionError(f"real beyond-EOF index was not rejected upstream: {volume_one}")
            stop_process(volume)
            volume = None

            code, repaired = run_helper(weed, dat, root / "stage-repair", "1", 1)
            if code != 0 or not repaired.get("success"):
                raise AssertionError(f"beyond-EOF repair failed: {repaired}")
            shutil.copyfile(repaired["candidate"], idx)
            os.chown(idx, UID, GID)
            volume = start_process(volume_command)
            repaired_status = wait_json(
                f"http://127.0.0.1:{VOLUME_PORT}/status",
                lambda item: any(volume.get("Id") == 1 for volume in item.get("Volumes") or []),
            )
            volume_one = next(item for item in repaired_status["Volumes"] if item["Id"] == 1)
            if volume_one.get("ReadOnly") is not False or int(volume_one.get("FileCount", 0)) != 2:
                raise AssertionError(f"upstream rejected the rebuilt real index: {volume_one}")
            stop_process(volume)
            volume = None

            # An incomplete final record must never be certified or truncated.
            truncated = data_dir / "2.dat"
            shutil.copyfile(dat, truncated)
            with truncated.open("r+b") as handle:
                handle.truncate(max(1, truncated.stat().st_size - 8))
            os.chown(truncated, UID, GID)
            truncated_size = truncated.stat().st_size
            truncated_hash = hashlib.sha256(truncated.read_bytes()).hexdigest()
            code, rejected = run_helper(weed, truncated, root / "stage-truncated", "2", 2)
            if code == 0 and rejected.get("success"):
                index_two = index_dir / "2.idx"
                shutil.copyfile(rejected["candidate"], index_two)
                os.chown(index_two, UID, GID)
                volume = start_process(volume_command)
                incomplete_status = wait_json(
                    f"http://127.0.0.1:{VOLUME_PORT}/status",
                    lambda item: isinstance(item.get("Volumes"), list),
                )
                volume_two = next(
                    (item for item in incomplete_status["Volumes"] if item.get("Id") == 2), None,
                )
                if volume_two is not None and volume_two.get("ReadOnly") is False:
                    raise AssertionError(f"upstream accepted an incomplete final record: {volume_two}")
                stop_process(volume)
                volume = None
            if truncated.stat().st_size != truncated_size:
                raise AssertionError("repair helper changed incomplete authoritative .dat size")
            if hashlib.sha256(truncated.read_bytes()).hexdigest() != truncated_hash:
                raise AssertionError("repair helper changed incomplete authoritative .dat contents")
            if hashlib.sha256(dat.read_bytes()).hexdigest() != original_dat_hash:
                raise AssertionError("repair helper changed the authoritative complete .dat")
        finally:
            stop_process(volume)
            stop_process(master)
    print("real SeaweedFS index repair checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
