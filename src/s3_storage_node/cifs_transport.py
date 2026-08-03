from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .config_types import TargetConfig
from .mounts import MountInfo, find_mount


class CifsTransportError(RuntimeError):
    pass


_DIALECT_ORDER = {
    "1.0": (1, 0, 0), "2.0": (2, 0, 0), "2.1": (2, 1, 0),
    "3.0": (3, 0, 0), "3.02": (3, 0, 2), "3.1.1": (3, 1, 1),
}
_DIALECT_HEX = {
    "0x100": "1.0", "0x202": "2.0", "0x210": "2.1",
    "0x300": "3.0", "0x302": "3.02", "0x311": "3.1.1",
}
_CAPABILITY_FAILURE_MARKERS = (
    "invalid argument", "operation not supported", "not supported",
    "unknown mount option", "unrecognized mount option",
)


def mount_option_name(option: str) -> str:
    return option.partition("=")[0].strip().lower()


def cifs_mount_options(target: TargetConfig) -> tuple[str, ...]:
    policy = target.effective_io_failure_policy
    if policy not in {"soft", "hard"}:
        raise CifsTransportError(f"invalid CIFS I/O failure policy for {target.name}: {policy}")
    if any(mount_option_name(option) in {"soft", "hard"} for option in target.mount_options):
        raise CifsTransportError(
            f"raw CIFS I/O failure options remain for {target.name}; use io_failure_policy instead"
        )
    reserved = {
        "persistenthandles", "resilienthandles", "nopersistenthandles",
        "noresilienthandles", "multichannel", "nomultichannel", "max_channels",
    }
    if any(mount_option_name(option) in reserved for option in target.mount_options):
        raise CifsTransportError(
            f"raw guardian-owned CIFS options remain for {target.name}; use explicit transport policy settings instead"
        )
    return (policy, *target.mount_options)


def cifs_mount_profiles(target: TargetConfig) -> tuple[tuple[str, ...], ...]:
    base = cifs_mount_options(target)
    if target.handle_reconnect_policy == "auto":
        handles: tuple[str | None, ...] = ("persistenthandles", "resilienthandles", None)
    elif target.handle_reconnect_policy in {"persistent", "resilient"}:
        handles = (f"{target.handle_reconnect_policy}handles",)
    elif target.handle_reconnect_policy == "disabled":
        handles = (None,)
    else:
        raise CifsTransportError(f"invalid handle reconnect policy for {target.name}: {target.handle_reconnect_policy}")

    enabled_channels = ("multichannel", f"max_channels={target.max_channels}")
    if target.multichannel_policy == "auto":
        channels: tuple[tuple[str, ...], ...] = (enabled_channels, ())
    elif target.multichannel_policy == "required":
        channels = (enabled_channels,)
    elif target.multichannel_policy == "disabled":
        channels = ((),)
    else:
        raise CifsTransportError(f"invalid multichannel policy for {target.name}: {target.multichannel_policy}")

    profiles: list[tuple[str, ...]] = []
    for handle in handles:
        for channel_options in channels:
            profile = (*base, *((handle,) if handle else ()), *channel_options)
            if profile not in profiles:
                profiles.append(profile)
    return tuple(profiles)


def is_capability_failure(error: Exception) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _CAPABILITY_FAILURE_MARKERS)


def _normalize_unc(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized.strip("/").lower()


def _normalize_dialect(value: str) -> str:
    candidate = value.strip().lower().rstrip(",")
    aliases = {"3.0.2": "3.02", "smb3": "3.0", "smb3.1.1": "3.1.1"}
    return _DIALECT_HEX.get(candidate, aliases.get(candidate, candidate))


def _option_value(options: Iterable[str], name: str) -> str:
    for option in options:
        key, separator, value = option.partition("=")
        if key.strip().lower() == name and separator:
            return value.strip()
    return ""


def parse_cifs_debug_data(text: str) -> dict[str, object]:
    result: dict[str, object] = {"shares": []}
    version = re.search(r"^CIFS Version\s+(.+)$", text, re.MULTILINE | re.IGNORECASE)
    if version:
        result["client_version"] = version.group(1).strip()
    dialects = re.findall(r"\bDialect(?:\s+|:)(0x[0-9a-fA-F]+|[0-9.]+)", text, re.IGNORECASE)
    if dialects:
        result["dialect"] = _normalize_dialect(dialects[-1])
    allocated = re.findall(r"Allocated channels:\s*(\d+)", text, re.IGNORECASE)
    if allocated:
        result["allocated_channels"] = int(allocated[-1])
    connected = len(re.findall(r"\[CONNECTED\]", text, re.IGNORECASE))
    if connected:
        result["connected_channels"] = connected
    tcp = [int(value) for value in re.findall(r"TCP status:\s*(\d+)", text, re.IGNORECASE)]
    if tcp:
        result["tcp_status"] = tcp[-1]

    pattern = re.compile(r"^\s*\d+\)\s+(\\\\\S+)(.*?)(?=^\s*\d+\)\s+|\Z)", re.MULTILINE | re.DOTALL)
    shares: list[dict[str, object]] = []
    for match in pattern.finditer(text):
        block = match.group(0)
        share: dict[str, object] = {"source": match.group(1)}
        status = re.search(r"\bStatus:\s*(\d+)", block, re.IGNORECASE)
        if status:
            share["status"] = int(status.group(1))
        shares.append(share)
    result["shares"] = shares
    return result


def parse_cifs_stats(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    reconnects = re.search(r"(\d+)\s+session\s+(\d+)\s+share reconnects", text, re.IGNORECASE)
    if reconnects:
        result["session_reconnects"] = int(reconnects.group(1))
        result["share_reconnects"] = int(reconnects.group(2))
    return result


def _read_optional(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        return None


def certify_cifs_transport(
    target: TargetConfig,
    *,
    mount: MountInfo | None = None,
    debug_data_path: str = "/proc/fs/cifs/DebugData",
    stats_path: str = "/proc/fs/cifs/Stats",
) -> dict[str, int | str]:
    if target.type != "cifs":
        return {}
    result: dict[str, int | str] = {
        "configured_io_failure_policy": target.effective_io_failure_policy,
        "configured_handle_reconnect_policy": target.handle_reconnect_policy,
        "effective_handle_reconnect_mode": "disabled",
        "configured_multichannel_policy": target.multichannel_policy,
        "effective_multichannel": 0,
        "transport_observed": 0,
    }
    current = mount or find_mount(target.mountpoint)
    if current is None:
        strict = bool(
            target.minimum_smb_dialect
            or target.handle_reconnect_policy in {"persistent", "resilient"}
            or target.multichannel_policy == "required"
            or target.require_transport_observability
        )
        if strict:
            raise CifsTransportError(f"cannot certify required CIFS transport state for {target.name}")
        return result

    options = current.mount_options | current.super_options
    names = {mount_option_name(option) for option in options}
    vers = _option_value(options, "vers")
    dialect = _normalize_dialect(vers) if vers else ""
    handle_mode = "persistent" if "persistenthandles" in names else "resilient" if "resilienthandles" in names else "disabled"
    multichannel_active = "multichannel" in names or "max_channels" in names
    result["effective_handle_reconnect_mode"] = handle_mode
    result["effective_multichannel"] = 1 if multichannel_active else 0

    debug_text = _read_optional(debug_data_path)
    matched_share: dict[str, object] | None = None
    if debug_text is not None:
        debug = parse_cifs_debug_data(debug_text)
        if debug.get("dialect"):
            dialect = str(debug["dialect"])
        expected = _normalize_unc(target.source)
        for share in debug.get("shares", []):
            if isinstance(share, dict) and _normalize_unc(str(share.get("source", ""))) == expected:
                matched_share = share
                break
        if matched_share is not None:
            result["transport_observed"] = 1
            if "status" in matched_share:
                result["share_status"] = int(matched_share["status"])
                if int(matched_share["status"]) == 0:
                    raise CifsTransportError(f"CIFS share for {target.name} is disconnected")
        for key in ("allocated_channels", "connected_channels", "tcp_status"):
            if key in debug:
                result[key] = int(debug[key])

    if dialect:
        result["effective_smb_dialect"] = dialect
    if target.minimum_smb_dialect:
        if not dialect:
            raise CifsTransportError(f"cannot certify minimum SMB dialect for {target.name}")
        if dialect not in _DIALECT_ORDER or _DIALECT_ORDER[dialect] < _DIALECT_ORDER[target.minimum_smb_dialect]:
            raise CifsTransportError(
                f"SMB dialect below minimum for {target.name}: {dialect} < {target.minimum_smb_dialect}"
            )
    if target.handle_reconnect_policy in {"persistent", "resilient"} and handle_mode != target.handle_reconnect_policy:
        raise CifsTransportError(f"required {target.handle_reconnect_policy} handles are not active for {target.name}")
    if target.multichannel_policy == "required":
        if not multichannel_active:
            raise CifsTransportError(f"required CIFS multichannel is not active for {target.name}")
        observed = int(result.get("connected_channels", result.get("allocated_channels", 0)))
        if observed and observed < 2:
            raise CifsTransportError(f"CIFS multichannel has fewer than two channels for {target.name}")
    if target.require_transport_observability and matched_share is None:
        raise CifsTransportError(f"CIFS transport telemetry is unavailable for {target.name}")

    stats_text = _read_optional(stats_path)
    if stats_text is not None:
        stats = parse_cifs_stats(stats_text)
        if "session_reconnects" in stats:
            result["cifs_session_reconnects"] = stats["session_reconnects"]
        if "share_reconnects" in stats:
            result["cifs_share_reconnects"] = stats["share_reconnects"]
    return result
