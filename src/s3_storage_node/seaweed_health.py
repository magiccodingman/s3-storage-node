from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class SeaweedHealthError(RuntimeError):
    pass


class UnexpectedReadonlyVolumes(SeaweedHealthError):
    """Upstream reported volumes that are not operator-certified read-only."""

    def __init__(self, status: dict[str, Any]) -> None:
        self.status = status
        unexpected = status.get("unexpected_readonly_volume_ids", [])
        super().__init__(
            "SeaweedFS reported unexpected read-only volumes: "
            + ",".join(str(volume_id) for volume_id in unexpected)
        )


def parse_volume_status(payload: object, expected_readonly_ids: set[int]) -> dict[str, Any]:
    if not isinstance(payload, dict) or "Volumes" not in payload:
        raise SeaweedHealthError("SeaweedFS volume status did not contain a Volumes array")
    raw_volumes = payload["Volumes"]
    if raw_volumes is None:
        raw_volumes = []
    if not isinstance(raw_volumes, list):
        raise SeaweedHealthError("SeaweedFS volume status did not contain a Volumes array")

    readonly_ids: list[int] = []
    unexpected_ids: list[int] = []
    volume_details: list[dict[str, Any]] = []
    collections: dict[str, dict[str, int]] = {}
    total = 0
    writable = 0
    for raw in raw_volumes:
        if not isinstance(raw, dict):
            raise SeaweedHealthError("SeaweedFS volume status contained an invalid volume entry")
        try:
            volume_id = int(raw["Id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SeaweedHealthError("SeaweedFS volume status contained an invalid volume Id") from exc
        readonly = raw.get("ReadOnly")
        if not isinstance(readonly, bool):
            raise SeaweedHealthError(f"SeaweedFS volume {volume_id} did not report a boolean ReadOnly state")
        raw_collection = str(raw.get("Collection") or "")
        collection_label = raw_collection or "<default>"
        counts = collections.setdefault(collection_label, {"total": 0, "readonly": 0, "writable": 0})
        counts["total"] += 1
        total += 1
        if readonly:
            counts["readonly"] += 1
            readonly_ids.append(volume_id)
            if volume_id not in expected_readonly_ids:
                unexpected_ids.append(volume_id)
        else:
            counts["writable"] += 1
            writable += 1
        volume_details.append({
            "id": volume_id,
            "collection": raw_collection,
            "readonly": readonly,
            "expected_readonly": readonly and volume_id in expected_readonly_ids,
        })

    readonly_ids.sort()
    unexpected_ids.sort()
    volume_details.sort(key=lambda volume: int(volume["id"]))
    return {
        "checked": True,
        "total": total,
        "readonly": len(readonly_ids),
        "writable": writable,
        "expected_readonly": len(readonly_ids) - len(unexpected_ids),
        "unexpected_readonly": len(unexpected_ids),
        "readonly_volume_ids": readonly_ids,
        "unexpected_readonly_volume_ids": unexpected_ids,
        "volume_details": volume_details,
        "collections": collections,
    }


def inspect_volume_status(
    host: str,
    port: int,
    *,
    expected_readonly_ids: set[int],
    timeout_seconds: float,
) -> dict[str, Any]:
    url = f"http://{host}:{port}/status"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise SeaweedHealthError(f"SeaweedFS volume status returned HTTP {response.status}")
            payload = json.load(response)
    except SeaweedHealthError:
        raise
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SeaweedHealthError(f"unable to read SeaweedFS volume status: {exc}") from exc
    return parse_volume_status(payload, expected_readonly_ids)
