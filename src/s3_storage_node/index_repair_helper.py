from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


FINGERPRINT_REGION_BYTES = 1024 * 1024
NEEDLE_MAP_ENTRY_BYTES = 16
NEEDLE_HEADER_BYTES = 16
NEEDLE_CHECKSUM_BYTES = 4
NEEDLE_TIMESTAMP_BYTES = 8
NEEDLE_PADDING_BYTES = 8


class RepairHelperError(RuntimeError):
    pass


def _needle_actual_size(size: int, version: int) -> int:
    """Match SeaweedFS needle.GetActualSize for an on-disk index entry."""

    payload_size = 0 if size < 0 else size
    trailer_size = NEEDLE_CHECKSUM_BYTES + (NEEDLE_TIMESTAMP_BYTES if version == 3 else 0)
    unpadded = NEEDLE_HEADER_BYTES + payload_size + trailer_size
    padding = NEEDLE_PADDING_BYTES - (unpadded % NEEDLE_PADDING_BYTES)
    return unpadded + padding


def inspect_index_bounds(index: Path, source: Path) -> dict[str, Any]:
    """Prove that every live candidate entry is physically contained in .dat."""

    source_info = source.stat()
    with source.open("rb", buffering=0) as handle:
        version_raw = handle.read(1)
    if not version_raw or version_raw[0] not in {1, 2, 3}:
        raise RepairHelperError(f"unsupported or missing SeaweedFS superblock in {source}")
    version = version_raw[0]
    index_size = index.stat().st_size
    if index_size % NEEDLE_MAP_ENTRY_BYTES:
        raise RepairHelperError(
            f"candidate index size {index_size} is not a multiple of {NEEDLE_MAP_ENTRY_BYTES}"
        )

    entry_count = 0
    max_end = 0
    max_valid_end = 0
    max_entry_offset = 0
    violations: list[dict[str, int]] = []
    with index.open("rb", buffering=0) as handle:
        while record := handle.read(NEEDLE_MAP_ENTRY_BYTES):
            if len(record) != NEEDLE_MAP_ENTRY_BYTES:
                raise RepairHelperError("candidate index ended with a partial entry")
            entry_count += 1
            needle_id = int.from_bytes(record[0:8], "big", signed=False)
            offset = int.from_bytes(record[8:12], "big", signed=False) * NEEDLE_PADDING_BYTES
            size = int.from_bytes(record[12:16], "big", signed=True)
            # SeaweedFS MaximumNeedleEnd deliberately ignores tombstones,
            # zero-sized values, and offset-zero remote-tier markers.
            if offset == 0 or size <= 0:
                continue
            end = offset + _needle_actual_size(size, version)
            max_entry_offset = max(max_entry_offset, offset)
            max_end = max(max_end, end)
            if end > source_info.st_size:
                violations.append({
                    "needle_id": needle_id,
                    "offset": offset,
                    "size": size,
                    "end": end,
                    "bytes_past_eof": end - source_info.st_size,
                })
            else:
                max_valid_end = max(max_valid_end, end)

    return {
        "version": version,
        "source_size": source_info.st_size,
        "index_size": index_size,
        "entry_count": entry_count,
        "maximum_needle_end": max_end,
        "maximum_valid_needle_end": max_valid_end,
        "maximum_entry_offset": max_entry_offset,
        "violations": violations,
        "valid": not violations,
    }


def fingerprint(path: Path, region_bytes: int = FINGERPRINT_REGION_BYTES) -> dict[str, Any]:
    """Fingerprint bounded regions without turning remote scans into full reads."""

    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise RepairHelperError(f"repair source is not a regular file: {path}")
    with path.open("rb", buffering=0) as handle:
        head = handle.read(region_bytes)
        tail_offset = max(0, before.st_size - region_bytes)
        handle.seek(tail_offset)
        tail = handle.read(region_bytes)
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise RepairHelperError(f"repair source changed while fingerprinting: {path}")
    return {
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "head_bytes": len(head),
        "head_sha256": hashlib.sha256(head).hexdigest(),
        "tail_offset": tail_offset,
        "tail_bytes": len(tail),
        "tail_sha256": hashlib.sha256(tail).hexdigest(),
    }


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mount_readonly(source: Path, target: Path) -> str:
    result = _run(["mount", "--bind", str(source), str(target)])
    if result.returncode != 0:
        raise RepairHelperError(result.stderr.strip() or "unable to bind-mount repair source")
    result = _run([
        "mount", "-o", "remount,bind,ro,nosuid,nodev,noexec", str(target),
    ])
    if result.returncode != 0:
        _run(["umount", str(target)])
        raise RepairHelperError(result.stderr.strip() or "unable to remount repair source read-only")
    result = _run(["findmnt", "--noheadings", "--output", "OPTIONS", "--target", str(target)])
    if result.returncode != 0:
        _run(["umount", str(target)])
        raise RepairHelperError(result.stderr.strip() or "unable to inspect repair source mount")
    options = result.stdout.strip().splitlines()[-1].strip()
    if "ro" not in {option.strip() for option in options.split(",")}:
        _run(["umount", str(target)])
        raise RepairHelperError(f"staged repair source is not read-only: {options}")
    return options


def _mount_masked_view(source_dir: Path, masked_dir: Path, uid: int, gid: int) -> str:
    result = _run([
        "bindfs",
        "--perms=u=rX,go=",
        f"--force-user={uid}",
        f"--force-group={gid}",
        "--chmod-deny", "--chown-deny", "--chgrp-deny",
        "--delete-deny", "--rename-deny", "--xattr-ro",
        "-o", "ro,nosuid,nodev,noexec,allow_other",
        str(source_dir), str(masked_dir),
    ])
    if result.returncode != 0:
        raise RepairHelperError(result.stderr.strip() or "unable to create permission-masked repair view")
    result = _run(["findmnt", "--noheadings", "--output", "FSTYPE,OPTIONS", "--target", str(masked_dir)])
    if result.returncode != 0:
        _run(["umount", str(masked_dir)])
        raise RepairHelperError(result.stderr.strip() or "unable to inspect permission-masked repair view")
    mount_state = result.stdout.strip().splitlines()[-1].strip()
    fields = mount_state.split(maxsplit=1)
    options = fields[1] if len(fields) > 1 else ""
    if fields[0] != "fuse" or "ro" not in {item.strip() for item in options.split(",")}:
        _run(["umount", str(masked_dir)])
        raise RepairHelperError(f"permission-masked repair view is not read-only FUSE: {mount_state}")
    return mount_state


def _prove_write_rejected(path: Path, uid: int, gid: int) -> None:
    command = [
        "setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--",
        sys.executable, "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).open('ab').write(b'repair-write-test')",
        str(path),
    ]
    result = _run(command)
    if result.returncode == 0:
        raise RepairHelperError("write unexpectedly succeeded through staged read-only .dat exposure")


def build_candidate(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source_dat)
    staging = Path(args.staging_dir)
    base_name = args.base_name
    staged_dat = staging / f"{base_name}.dat"
    candidate = staging / f"{base_name}.idx"
    source_dir = staging / ".source"
    masked_dir = staging / ".masked"
    source_bind = source_dir / f"{base_name}.dat"
    result: dict[str, Any] = {
        "source": str(source),
        "candidate": str(candidate),
        "readonly_mount_verified": False,
        "write_rejected": False,
    }
    source_mounted = False
    masked_mounted = False
    staged_mounted = False
    try:
        if source.is_symlink():
            raise RepairHelperError("repair source may not be a symlink")
        source_info = source.stat()
        if not stat.S_ISREG(source_info.st_mode):
            raise RepairHelperError(f"repair source is not a regular file: {source}")
        staging.mkdir(parents=True, exist_ok=True)
        os.chown(staging, args.uid, args.gid)
        os.chmod(staging, 0o750)
        for stale_file in (staged_dat, source_bind):
            if stale_file.is_symlink():
                raise RepairHelperError(f"unsafe stale repair path: {stale_file}")
            stale_file.unlink(missing_ok=True)
        for stale_dir in (masked_dir, source_dir):
            try:
                stale_dir.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RepairHelperError(f"stale repair staging directory is not empty: {stale_dir}") from exc
        source_dir.mkdir(mode=0o700)
        masked_dir.mkdir(mode=0o700)
        source_bind.touch(mode=0o400)
        result["source_fingerprint_before"] = fingerprint(source)
        result["source_mode_before"] = source_info.st_mode & 0o7777
        result["source_uid_before"] = source_info.st_uid
        result["source_gid_before"] = source_info.st_gid
        result["source_bind_mount_options"] = _mount_readonly(source, source_bind)
        source_mounted = True
        result["masked_view_mount"] = _mount_masked_view(source_dir, masked_dir, args.uid, args.gid)
        masked_mounted = True
        staged_dat.touch(mode=0o400)
        result["mount_options"] = _mount_readonly(masked_dir / f"{base_name}.dat", staged_dat)
        staged_mounted = True
        result["readonly_mount_verified"] = True
        _prove_write_rejected(staged_dat, args.uid, args.gid)
        result["write_rejected"] = True
        before = fingerprint(staged_dat)
        if before != result["source_fingerprint_before"]:
            raise RepairHelperError("staged read-only view does not match the authoritative .dat fingerprint")

        if args.fingerprint_only:
            source_after_info = source.stat()
            result["source_fingerprint_after"] = fingerprint(source)
            if (
                (source_after_info.st_mode & 0o7777) != result["source_mode_before"]
                or source_after_info.st_uid != result["source_uid_before"]
                or source_after_info.st_gid != result["source_gid_before"]
            ):
                raise RepairHelperError("authoritative .dat ownership or permissions changed during staging")
            result["success"] = True
            return result

        command = [args.weed_binary, "fix", f"-volumeId={args.volume_id}"]
        if args.collection:
            command.append(f"-collection={args.collection}")
        command.append(str(staging))
        weed = _run([
            "setpriv", f"--reuid={args.uid}", f"--regid={args.gid}", "--clear-groups", "--",
            *command,
        ])
        result["weed_exit_code"] = weed.returncode
        result["weed_stdout"] = weed.stdout[-4096:]
        result["weed_stderr"] = weed.stderr[-4096:]
        after = fingerprint(source)
        result["source_fingerprint_after"] = after
        source_after_info = source.stat()
        if (
            (source_after_info.st_mode & 0o7777) != result["source_mode_before"]
            or source_after_info.st_uid != result["source_uid_before"]
            or source_after_info.st_gid != result["source_gid_before"]
        ):
            candidate.unlink(missing_ok=True)
            raise RepairHelperError("authoritative .dat ownership or permissions changed during reconstruction")
        if before != after:
            candidate.unlink(missing_ok=True)
            raise RepairHelperError("authoritative .dat changed during candidate reconstruction")
        if weed.returncode != 0:
            candidate.unlink(missing_ok=True)
            raise RepairHelperError(
                weed.stderr.strip() or weed.stdout.strip() or f"weed fix exited with {weed.returncode}"
            )
        if not candidate.is_file() or candidate.is_symlink():
            raise RepairHelperError(f"weed fix did not create the expected candidate: {candidate}")
        bounds = inspect_index_bounds(candidate, source)
        result["candidate_bounds"] = bounds
        if not bounds["valid"]:
            candidate.unlink(missing_ok=True)
            first = bounds["violations"][0]
            result["manual_intervention_required"] = True
            raise RepairHelperError(
                "candidate index references bytes past authoritative .dat EOF: "
                f"needle {first['needle_id']} ends {first['bytes_past_eof']} bytes past EOF"
            )
        with candidate.open("rb") as handle:
            candidate_hash = hashlib.file_digest(handle, "sha256").hexdigest()
            os.fsync(handle.fileno())
        _fsync_directory(staging)
        result["candidate_size"] = candidate.stat().st_size
        result["candidate_sha256"] = candidate_hash
        result["success"] = True
        return result
    except (OSError, RepairHelperError) as exc:
        result["success"] = False
        result["error"] = str(exc)
        return result
    finally:
        if staged_mounted:
            unmount = _run(["umount", str(staged_dat)])
            if unmount.returncode != 0:
                result["success"] = False
                result["error"] = unmount.stderr.strip() or "unable to unmount staged repair source"
        staged_dat.unlink(missing_ok=True)
        if masked_mounted:
            unmount = _run(["umount", str(masked_dir)])
            if unmount.returncode != 0:
                result["success"] = False
                result["error"] = unmount.stderr.strip() or "unable to unmount permission-masked repair view"
        if source_mounted:
            unmount = _run(["umount", str(source_bind)])
            if unmount.returncode != 0:
                result["success"] = False
                result["error"] = unmount.stderr.strip() or "unable to unmount authoritative repair source"
        source_bind.unlink(missing_ok=True)
        for directory in (masked_dir, source_dir):
            try:
                directory.rmdir()
            except OSError:
                result["success"] = False
                result["error"] = f"unable to remove private repair mount directory: {directory}"


def recover_incomplete_tail(args: argparse.Namespace) -> dict[str, Any]:
    """Preserve and remove one independently proven incomplete final record."""

    result = build_candidate(args)
    bounds = result.get("candidate_bounds")
    source = Path(args.source_dat)
    live_index = Path(args.live_index)
    tail_backup = Path(args.tail_backup)
    try:
        expected = json.loads(args.expected_fingerprint)
        if not isinstance(expected, dict):
            raise RepairHelperError("expected source fingerprint is not an object")
        if result.get("success") or not result.get("manual_intervention_required"):
            raise RepairHelperError("source does not contain a rejected incomplete-tail candidate")
        if not isinstance(bounds, dict):
            raise RepairHelperError("candidate bounds are unavailable")
        violations = bounds.get("violations")
        if not isinstance(violations, list) or len(violations) != 1:
            raise RepairHelperError("candidate does not contain exactly one incomplete record")
        violation = violations[0]
        if not isinstance(violation, dict):
            raise RepairHelperError("candidate violation is malformed")
        offset = int(violation["offset"])
        source_size = int(bounds["source_size"])
        tail_size = source_size - offset
        if (
            offset <= 0
            or tail_size <= 0
            or tail_size > args.maximum_tail_bytes
            or int(bounds["maximum_entry_offset"]) != offset
            or int(bounds["maximum_valid_needle_end"]) > offset
        ):
            raise RepairHelperError("candidate does not prove a bounded final-record tail")
        live_bounds = inspect_index_bounds(live_index, source)
        if not live_bounds["valid"] or int(live_bounds["maximum_needle_end"]) > offset:
            raise RepairHelperError("live index is inconsistent with the proven tail boundary")
        if fingerprint(source) != expected:
            raise RepairHelperError("authoritative source fingerprint changed before tail recovery")

        with source.open("rb", buffering=0) as source_handle:
            source_handle.seek(offset)
            tail = source_handle.read(tail_size + 1)
        if len(tail) != tail_size:
            raise RepairHelperError("could not read the complete incomplete-record tail")
        tail_hash = hashlib.sha256(tail).hexdigest()
        tail_backup.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(tail_backup.parent, 0o700)
        if tail_backup.is_symlink():
            raise RepairHelperError("tail backup may not be a symlink")
        if tail_backup.exists():
            backup_info = tail_backup.stat()
            if not stat.S_ISREG(backup_info.st_mode):
                raise RepairHelperError("tail backup is not a regular file")
            with tail_backup.open("rb") as backup_handle:
                existing_hash = hashlib.file_digest(backup_handle, "sha256").hexdigest()
            if backup_info.st_size != tail_size or existing_hash != tail_hash:
                raise RepairHelperError("existing tail backup does not match the authoritative tail")
        else:
            with tail_backup.open("xb") as backup_handle:
                backup_handle.write(tail)
                backup_handle.flush()
                os.fsync(backup_handle.fileno())
            os.chmod(tail_backup, 0o600)
            _fsync_directory(tail_backup.parent)
        with tail_backup.open("rb") as backup_handle:
            verified_hash = hashlib.file_digest(backup_handle, "sha256").hexdigest()
        if verified_hash != tail_hash:
            raise RepairHelperError("tail backup hash verification failed")
        if fingerprint(source) != expected:
            raise RepairHelperError("authoritative source fingerprint changed after tail backup")
        with source.open("r+b", buffering=0) as source_handle:
            source_handle.truncate(offset)
            source_handle.flush()
            os.fsync(source_handle.fileno())
        recovered = fingerprint(source)
        if int(recovered["size"]) != offset:
            raise RepairHelperError("incomplete-tail truncation did not reach the proven boundary")
        return {
            "success": True,
            "tail_recovered": True,
            "offset": offset,
            "tail_size": tail_size,
            "tail_sha256": tail_hash,
            "tail_backup": str(tail_backup),
            "needle_id": int(violation["needle_id"]),
            "source_fingerprint_after": recovered,
        }
    except (OSError, RepairHelperError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"success": False, "error": str(exc), "tail_recovered": False}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="s3-storage-node-index-repair-helper")
    result.add_argument("--source-dat", required=True)
    result.add_argument("--staging-dir", required=True)
    result.add_argument("--base-name", required=True)
    result.add_argument("--collection", default="")
    result.add_argument("--volume-id", required=True, type=int)
    result.add_argument("--weed-binary", required=True)
    result.add_argument("--uid", required=True, type=int)
    result.add_argument("--gid", required=True, type=int)
    result.add_argument("--fingerprint-only", action="store_true")
    result.add_argument("--recover-incomplete-tail", action="store_true")
    result.add_argument("--live-index", default="")
    result.add_argument("--tail-backup", default="")
    result.add_argument("--expected-fingerprint", default="")
    result.add_argument("--maximum-tail-bytes", type=int, default=0)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    result = recover_incomplete_tail(args) if args.recover_incomplete_tail else build_candidate(args)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
