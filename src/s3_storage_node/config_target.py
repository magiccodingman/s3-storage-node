from __future__ import annotations

from pathlib import Path
from typing import Any

from .config_helpers import ConfigError, _bool, _int, _positive, _string, _string_list
from .config_types import TargetConfig

_CIFS_IO_FAILURE_POLICIES = {"soft", "hard"}
_CIFS_HANDLE_POLICIES = {"disabled", "auto", "resilient", "persistent"}
_CIFS_MULTICHANNEL_POLICIES = {"disabled", "auto", "required"}
_SMB_DIALECTS = {"1.0", "2.0", "2.1", "3.0", "3.02", "3.1.1"}
_HANDLE_OPTIONS = {"persistenthandles", "resilienthandles", "nopersistenthandles", "noresilienthandles"}
_MULTICHANNEL_OPTIONS = {"multichannel", "nomultichannel", "max_channels"}


def _mount_option_name(option: str) -> str:
    return option.partition("=")[0].strip().lower()


def _normalize_dialect(value: str) -> str:
    value = value.strip().lower()
    aliases = {"3.0.2": "3.02", "smb3": "3.0", "smb3.1.1": "3.1.1"}
    return aliases.get(value, value)


def _parse_cifs_io_failure_policy(
    name: str,
    raw: dict[str, Any],
    mount_options: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    configured = _string(raw.get("io_failure_policy"), f"storage.{name}.io_failure_policy", "").strip().lower()
    if configured and configured not in _CIFS_IO_FAILURE_POLICIES:
        raise ConfigError(f"storage.{name}.io_failure_policy must be soft or hard")

    legacy_policies = {
        _mount_option_name(option)
        for option in mount_options
        if _mount_option_name(option) in _CIFS_IO_FAILURE_POLICIES
    }
    if len(legacy_policies) > 1:
        raise ConfigError(f"storage.{name}.mount_options may not contain both soft and hard")

    legacy = next(iter(legacy_policies), "")
    if configured and legacy and configured != legacy:
        raise ConfigError(
            f"storage.{name}.io_failure_policy={configured} conflicts with mount_options containing {legacy}"
        )

    policy = configured or legacy or "soft"
    remaining_options = tuple(
        option for option in mount_options if _mount_option_name(option) not in _CIFS_IO_FAILURE_POLICIES
    )
    return policy, remaining_options


def _legacy_handle_policy(name: str, mount_options: tuple[str, ...]) -> str:
    names = {_mount_option_name(option) for option in mount_options}
    positive = names & {"persistenthandles", "resilienthandles"}
    negative = names & {"nopersistenthandles", "noresilienthandles"}
    if len(positive) > 1:
        raise ConfigError(f"storage.{name}.mount_options may not contain both persistenthandles and resilienthandles")
    if positive and negative:
        raise ConfigError(f"storage.{name}.mount_options contains contradictory handle reconnect options")
    if "persistenthandles" in positive:
        return "persistent"
    if "resilienthandles" in positive:
        return "resilient"
    if negative:
        return "disabled"
    return ""


def _legacy_multichannel_policy(name: str, mount_options: tuple[str, ...]) -> tuple[str, int | None]:
    names = [_mount_option_name(option) for option in mount_options]
    positive = any(option in {"multichannel", "max_channels"} for option in names)
    negative = "nomultichannel" in names
    if positive and negative:
        raise ConfigError(f"storage.{name}.mount_options contains contradictory multichannel options")
    values = [option.partition("=")[2].strip() for option in mount_options if _mount_option_name(option) == "max_channels"]
    if len(values) > 1:
        raise ConfigError(f"storage.{name}.mount_options may contain max_channels only once")
    legacy_max: int | None = None
    if values:
        try:
            legacy_max = int(values[0])
        except ValueError as exc:
            raise ConfigError(f"storage.{name}.mount_options max_channels must be an integer") from exc
    if positive:
        return "required", legacy_max
    if negative:
        return "disabled", legacy_max
    return "", legacy_max


def _parse_cifs_transport_policy(
    name: str,
    raw: dict[str, Any],
    mount_options: tuple[str, ...],
) -> tuple[str, str, str, int, bool, tuple[str, ...]]:
    minimum_dialect = _normalize_dialect(
        _string(raw.get("minimum_smb_dialect"), f"storage.{name}.minimum_smb_dialect", "")
    )
    if minimum_dialect and minimum_dialect not in _SMB_DIALECTS:
        raise ConfigError(
            f"storage.{name}.minimum_smb_dialect must be one of {', '.join(sorted(_SMB_DIALECTS))}"
        )

    configured_handle = _string(
        raw.get("handle_reconnect_policy"), f"storage.{name}.handle_reconnect_policy", ""
    ).strip().lower()
    if configured_handle and configured_handle not in _CIFS_HANDLE_POLICIES:
        raise ConfigError(
            f"storage.{name}.handle_reconnect_policy must be disabled, auto, resilient, or persistent"
        )
    legacy_handle = _legacy_handle_policy(name, mount_options)
    if configured_handle and legacy_handle and configured_handle != legacy_handle:
        raise ConfigError(
            f"storage.{name}.handle_reconnect_policy={configured_handle} conflicts with legacy mount_options"
        )
    handle_policy = configured_handle or legacy_handle or "disabled"

    configured_multichannel = _string(
        raw.get("multichannel_policy"), f"storage.{name}.multichannel_policy", ""
    ).strip().lower()
    if configured_multichannel and configured_multichannel not in _CIFS_MULTICHANNEL_POLICIES:
        raise ConfigError(f"storage.{name}.multichannel_policy must be disabled, auto, or required")
    legacy_multichannel, legacy_max = _legacy_multichannel_policy(name, mount_options)
    if configured_multichannel and legacy_multichannel and configured_multichannel != legacy_multichannel:
        raise ConfigError(
            f"storage.{name}.multichannel_policy={configured_multichannel} conflicts with legacy mount_options"
        )
    multichannel_policy = configured_multichannel or legacy_multichannel or "disabled"
    max_channels = _int(raw.get("max_channels"), f"storage.{name}.max_channels", legacy_max or 2)
    if not 2 <= max_channels <= 16:
        raise ConfigError(f"storage.{name}.max_channels must be between 2 and 16")
    if multichannel_policy == "disabled" and ("max_channels" in raw or legacy_max is not None):
        raise ConfigError(f"storage.{name}.max_channels requires multichannel_policy=auto or required")

    require_observability = _bool(
        raw.get("require_transport_observability"),
        f"storage.{name}.require_transport_observability",
        False,
    )
    reserved = _HANDLE_OPTIONS | _MULTICHANNEL_OPTIONS
    remaining = tuple(option for option in mount_options if _mount_option_name(option) not in reserved)
    return minimum_dialect, handle_policy, multichannel_policy, max_channels, require_observability, remaining


def _parse_target(name: str, raw: dict[str, Any], runtime_dir: Path) -> TargetConfig:
    target_type = _string(raw.get("type"), f"storage.{name}.type", "path").lower()
    if target_type not in {"cifs", "block", "path"}:
        raise ConfigError(f"storage.{name}.type must be cifs, block, or path")

    default_mountpoint = runtime_dir / "mounts" / name
    mountpoint = Path(_string(raw.get("mountpoint"), f"storage.{name}.mountpoint", str(default_mountpoint)))
    if not mountpoint.is_absolute():
        raise ConfigError(f"storage.{name}.mountpoint must be absolute")

    subdirectory = _string(raw.get("subdirectory"), f"storage.{name}.subdirectory", "")
    if subdirectory and (Path(subdirectory).is_absolute() or ".." in Path(subdirectory).parts):
        raise ConfigError(f"storage.{name}.subdirectory must be relative and may not contain '..'")

    sentinel_file = _string(raw.get("sentinel_file"), f"storage.{name}.sentinel_file", ".s3-storage-node.json")
    if not sentinel_file or Path(sentinel_file).is_absolute() or len(Path(sentinel_file).parts) != 1:
        raise ConfigError(f"storage.{name}.sentinel_file must be a single relative filename")

    mount_options = tuple(_string_list(raw.get("mount_options"), f"storage.{name}.mount_options"))
    io_failure_policy = ""
    minimum_smb_dialect = ""
    handle_reconnect_policy = "disabled"
    multichannel_policy = "disabled"
    max_channels = 2
    require_transport_observability = False
    cifs_only = {
        "io_failure_policy", "minimum_smb_dialect", "handle_reconnect_policy",
        "multichannel_policy", "max_channels", "require_transport_observability",
    }
    if target_type == "cifs":
        io_failure_policy, mount_options = _parse_cifs_io_failure_policy(name, raw, mount_options)
        (
            minimum_smb_dialect,
            handle_reconnect_policy,
            multichannel_policy,
            max_channels,
            require_transport_observability,
            mount_options,
        ) = _parse_cifs_transport_policy(name, raw, mount_options)
    elif cifs_only.intersection(raw):
        key = sorted(cifs_only.intersection(raw))[0]
        raise ConfigError(f"storage.{name}.{key} is only valid for CIFS targets")

    target = TargetConfig(
        name=name,
        type=target_type,
        mountpoint=mountpoint,
        subdirectory=subdirectory,
        sentinel_id=_string(raw.get("sentinel_id"), f"storage.{name}.sentinel_id", ""),
        sentinel_file=sentinel_file,
        allow_initialize=_bool(raw.get("allow_initialize"), f"storage.{name}.allow_initialize", False),
        min_free_bytes=_int(raw.get("min_free_bytes"), f"storage.{name}.min_free_bytes", 1_073_741_824),
        source=_string(raw.get("source"), f"storage.{name}.source", ""),
        credentials_file=_string(raw.get("credentials_file"), f"storage.{name}.credentials_file", ""),
        mount_options=mount_options,
        io_failure_policy=io_failure_policy,
        minimum_smb_dialect=minimum_smb_dialect,
        handle_reconnect_policy=handle_reconnect_policy,
        multichannel_policy=multichannel_policy,
        max_channels=max_channels,
        require_transport_observability=require_transport_observability,
        device=_string(raw.get("device"), f"storage.{name}.device", ""),
        expected_uuid=_string(raw.get("expected_uuid"), f"storage.{name}.expected_uuid", ""),
        expected_filesystem=_string(raw.get("expected_filesystem"), f"storage.{name}.expected_filesystem", ""),
    )

    if not target.sentinel_id:
        raise ConfigError(f"storage.{name}.sentinel_id is required")
    _positive(target.min_free_bytes, f"storage.{name}.min_free_bytes", allow_zero=True)

    if target.type == "cifs":
        if not target.source.startswith("//"):
            raise ConfigError(f"storage.{name}.source must look like //server/share")
        if not target.credentials_file:
            raise ConfigError(f"storage.{name}.credentials_file is required for CIFS")
        managed_root = runtime_dir / "mounts"
        if not mountpoint.is_relative_to(managed_root):
            raise ConfigError(f"storage.{name}.mountpoint for CIFS must be beneath {managed_root}")
    elif target.type == "block":
        if not target.device:
            raise ConfigError(f"storage.{name}.device is required for block storage")
        if not target.expected_uuid:
            raise ConfigError(f"storage.{name}.expected_uuid is required for block storage")
        if not target.expected_filesystem:
            raise ConfigError(f"storage.{name}.expected_filesystem is required for block storage")
        managed_root = runtime_dir / "mounts"
        if not mountpoint.is_relative_to(managed_root):
            raise ConfigError(f"storage.{name}.mountpoint for block storage must be beneath {managed_root}")
    elif not raw.get("mountpoint"):
        raise ConfigError(f"storage.{name}.mountpoint is required for path storage")

    return target
