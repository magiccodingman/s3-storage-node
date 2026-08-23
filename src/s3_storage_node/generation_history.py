from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class GenerationHistory:
    """Crash-safe, bounded history explaining why generations turn over."""

    MAX_HISTORY = 64

    def __init__(self, control_dir: Path) -> None:
        self.path = control_dir / "generation-history.json"
        self._state = self._load()

    def recover_interrupted_active(self) -> bool:
        """Close a prior active record after the caller owns the writer lease."""

        if isinstance(self._state.get("active"), dict):
            self._finish_active(
                cause="guardian_restart_detected",
                reason="guardian restarted before the prior generation recorded an outcome",
                phase="startup",
                clean_shutdown=False,
                fence_verified=False,
            )
            self._persist()
            return True
        return False

    def _load(self) -> dict[str, Any]:
        uncertain = False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {}
        except (OSError, json.JSONDecodeError):
            payload = {}
            uncertain = True
        if not isinstance(payload, dict):
            payload = {}
            uncertain = True
        if not isinstance(payload.get("counters", {}), dict):
            payload["counters"] = {}
            uncertain = True
        if not isinstance(payload.get("history", []), list):
            payload["history"] = []
            uncertain = True
        if payload.get("active") is not None and not isinstance(payload.get("active"), dict):
            payload["active"] = None
            uncertain = True
        if "indexes_certified" in payload and not isinstance(payload["indexes_certified"], bool):
            payload["indexes_certified"] = False
            uncertain = True
        payload.setdefault("version", 1)
        if uncertain:
            payload["indexes_certified"] = False
            payload["index_certification_reason"] = "generation history was unreadable"
            payload["index_certified_at"] = 0.0
        else:
            payload.setdefault("indexes_certified", True)
            payload.setdefault("index_certification_reason", "no unclean generation recorded")
            payload.setdefault("index_certified_at", 0.0)
        payload.setdefault("counters", {})
        payload.setdefault("history", [])
        payload.setdefault("active", None)
        return payload

    @staticmethod
    def classify_cause(exc: BaseException, phase: str) -> str:
        name = type(exc).__name__
        if name == "TransportSwitchRequested":
            return "operator_transport_switch"
        if name == "StorageError":
            return "storage_failure"
        if name == "ProcessError":
            return "process_failure"
        if name in {"SeaweedHealthError", "UnexpectedReadonlyVolumes"}:
            return "seaweed_volume_health_failure"
        if name in {
            "IndexRepairError", "IndexRepairJournalError", "IndexRepairManualIntervention",
            "IndexValidationRequired",
        }:
            return "seaweed_index_repair_failure"
        if name == "S3CheckError":
            return "s3_canary_failure"
        if phase in {"MOUNTING", "VERIFYING_STORAGE", "VERIFYING_TRANSPORTS"}:
            return "storage_startup_failure"
        if phase == "STARTING_SEAWEED":
            return "seaweed_startup_failure"
        if phase == "CREATING_GENERATION":
            return "generation_creation_failure"
        return "runtime_failure"

    def start(self, generation: int, *, transport: str, mode: str) -> None:
        if isinstance(self._state.get("active"), dict):
            self._finish_active(
                cause="superseded_without_outcome",
                reason="a replacement generation started without a recorded prior outcome",
                phase="creating_generation",
                clean_shutdown=False,
                fence_verified=False,
            )
        self._increment("generations_created_total")
        self._state["active"] = {
            "generation": generation,
            "transport": transport,
            "mode": mode,
            "started_at": time.time(),
        }
        self._persist()

    def finish(
        self,
        *,
        cause: str,
        reason: str,
        phase: str,
        clean_shutdown: bool,
        fence_verified: bool,
    ) -> None:
        if not isinstance(self._state.get("active"), dict):
            return
        self._finish_active(
            cause=cause,
            reason=reason,
            phase=phase,
            clean_shutdown=clean_shutdown,
            fence_verified=fence_verified,
        )
        self._persist()

    def _finish_active(
        self,
        *,
        cause: str,
        reason: str,
        phase: str,
        clean_shutdown: bool,
        fence_verified: bool,
    ) -> None:
        active = dict(self._state.get("active") or {})
        ended_at = time.time()
        active.update({
            "ended_at": ended_at,
            "duration_seconds": max(0.0, ended_at - float(active.get("started_at", ended_at))),
            "cause": cause,
            "reason": reason,
            "phase": phase,
            "clean_shutdown": clean_shutdown,
            "fence_verified": fence_verified,
        })
        history = list(self._state.get("history") or [])
        history.append(active)
        self._state["history"] = history[-self.MAX_HISTORY:]
        self._state["active"] = None
        self._increment("generations_completed_total")
        self._increment(f"cause:{cause}")
        if clean_shutdown:
            self._increment("clean_shutdowns_total")
        else:
            self._increment("unclean_shutdowns_total")
            self._state["indexes_certified"] = False
            self._state["index_certification_reason"] = (
                f"generation {active.get('generation', 0)} ended uncleanly: {reason}"
            )
            self._state["index_certified_at"] = 0.0
        if fence_verified:
            self._increment("verified_fences_total")
        else:
            self._increment("unverified_fences_total")

    def certify_indexes(self, reason: str) -> None:
        self._state["indexes_certified"] = True
        self._state["index_certification_reason"] = reason
        self._state["index_certified_at"] = time.time()
        self._increment("index_certifications_total")
        self._persist()

    def _increment(self, name: str) -> None:
        counters = self._state.setdefault("counters", {})
        counters[name] = int(counters.get(name, 0)) + 1

    def _persist(self) -> None:
        _write_json_atomic(self.path, self._state)

    def snapshot(self) -> dict[str, Any]:
        history = list(self._state.get("history") or [])
        return {
            "indexes_certified": bool(self._state.get("indexes_certified", False)),
            "index_certification_reason": str(self._state.get("index_certification_reason", "")),
            "index_certified_at": float(self._state.get("index_certified_at", 0.0)),
            "counters": dict(self._state.get("counters") or {}),
            "active": dict(self._state["active"]) if isinstance(self._state.get("active"), dict) else None,
            "recent": history[-10:],
        }
