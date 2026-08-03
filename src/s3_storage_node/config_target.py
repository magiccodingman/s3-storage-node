from __future__ import annotations

from pathlib import Path
from typing import Any

from .config_helpers import ConfigError, _bool, _int, _positive, _string, _string_list
from .config_types import TargetConfig

_CIFS_IO_FAILURE_POLICIES = {"soft", "hard"}


def _mount_option_name(option: str) -> str:
    return option.partition("=")[0].strip().lower()


def _parse_cifs_io_failure_policy(
    name: str,
    raw: dict[str, Any],
    mount_options: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    configured = _string(
        raw.get("io_failure_policy"),
        f"storage.{name}.io_failure_policy",
        "",
    ).strip().lower()
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
        option
        for option in mount_options
        if _mount_option_name(option) not in _CIFS_IO_FAILURE_POLICIES
    )
    return policy, remaining_options


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
    if target_type == "cifs":
        io_failure_policy, mount_options = _parse_cifs_io_failure_policy(name, raw, mount_options)
    elif "io_failure_policy" in raw:
        raise ConfigError(f"storage.{name}.io_failure_policy is only valid for CIFS targets")

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
