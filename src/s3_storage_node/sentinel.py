from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .config_types import TargetConfig


SENTINEL_SCHEMA = "s3-storage-node/dataset-sentinel"
CURRENT_SENTINEL_VERSION = 2


class SentinelError(ValueError):
    pass


@dataclass(frozen=True)
class SentinelIdentity:
    version: int
    schema: str
    dataset_id: str
    target: str
    subdirectory: str
    legacy: bool = False

    def health_fields(self) -> dict[str, int | str]:
        return {
            "sentinel_version": self.version,
            "sentinel_schema": self.schema,
            "dataset_id": self.dataset_id,
        }


def sentinel_v2_payload(target: TargetConfig) -> dict[str, object]:
    """Build a transport-independent identity document for one logical dataset."""

    return {
        "schema": SENTINEL_SCHEMA,
        "version": CURRENT_SENTINEL_VERSION,
        # Keep sentinel_id for old operators and tooling while dataset_id becomes
        # the explicit logical identity used by V2 validation.
        "sentinel_id": target.sentinel_id,
        "dataset_id": target.sentinel_id,
        "target": target.name,
        "subdirectory": target.subdirectory,
        # A V2 sentinel intentionally does not name CIFS, SSHFS, a host, or a
        # mountpoint. Multiple exclusive transports may expose this same dataset.
        "transport_independent": True,
    }


def _string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise SentinelError(f"sentinel field {field!r} must be a non-empty string")
    return value


def validate_sentinel_payload(target: TargetConfig, payload: object) -> SentinelIdentity:
    if not isinstance(payload, Mapping):
        raise SentinelError("sentinel document must be a JSON object")

    version = payload.get("version", 1)
    if type(version) is not int:  # bool is deliberately not accepted as an integer version.
        raise SentinelError("sentinel version must be an integer")

    if version == 1:
        sentinel_id = _string(payload, "sentinel_id")
        if sentinel_id != target.sentinel_id:
            raise SentinelError(
                f"sentinel mismatch for {target.name}: expected {target.sentinel_id}, got {sentinel_id}"
            )
        legacy_target = payload.get("target", "")
        if legacy_target is not None and not isinstance(legacy_target, str):
            raise SentinelError("legacy sentinel target must be a string when present")
        return SentinelIdentity(
            version=1,
            schema="legacy-v1",
            dataset_id=sentinel_id,
            target=legacy_target or target.name,
            subdirectory=target.subdirectory,
            legacy=True,
        )

    if version != CURRENT_SENTINEL_VERSION:
        raise SentinelError(
            f"unsupported sentinel version {version}; supported versions are 1 and {CURRENT_SENTINEL_VERSION}"
        )

    schema = _string(payload, "schema")
    if schema != SENTINEL_SCHEMA:
        raise SentinelError(f"unexpected sentinel schema: {schema}")
    sentinel_id = _string(payload, "sentinel_id")
    dataset_id = _string(payload, "dataset_id")
    target_name = _string(payload, "target")
    subdirectory = payload.get("subdirectory")
    if not isinstance(subdirectory, str):
        raise SentinelError("sentinel field 'subdirectory' must be a string")
    if payload.get("transport_independent") is not True:
        raise SentinelError("sentinel V2 must declare transport_independent=true")

    if sentinel_id != target.sentinel_id or dataset_id != target.sentinel_id:
        observed = dataset_id if dataset_id != target.sentinel_id else sentinel_id
        raise SentinelError(
            f"sentinel dataset mismatch for {target.name}: expected {target.sentinel_id}, got {observed}"
        )
    if sentinel_id != dataset_id:
        raise SentinelError("sentinel_id and dataset_id must identify the same logical dataset")
    if target_name != target.name:
        raise SentinelError(
            f"sentinel target mismatch: expected {target.name}, got {target_name}"
        )
    if subdirectory != target.subdirectory:
        raise SentinelError(
            f"sentinel subdirectory mismatch for {target.name}: expected {target.subdirectory!r}, got {subdirectory!r}"
        )

    return SentinelIdentity(
        version=CURRENT_SENTINEL_VERSION,
        schema=schema,
        dataset_id=dataset_id,
        target=target_name,
        subdirectory=subdirectory,
    )
