from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .logging import event


TERMINAL_PHASES = {"verified", "manual_intervention_required", "resolved_without_install"}
PENDING_VALIDATION_PHASES = {"candidate_installed", "awaiting_upstream_validation"}
PREINSTALL_PHASES = {
    "detected", "writers_stopped", "source_mounted_readonly", "candidate_building",
    "candidate_built", "backup_created", "failed_preinstall",
}
PREINSTALL_RETRY_LIMIT = 2


class IndexRepairError(RuntimeError):
    pass


class IndexRepairJournalError(IndexRepairError):
    pass


class IndexRepairManualIntervention(IndexRepairError):
    pass


class IndexValidationRequired(IndexRepairError):
    def __init__(self, status: dict[str, Any]) -> None:
        self.status = status
        super().__init__("installed index candidates require upstream validation")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _hash_file(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    info = path.stat()
    return {"size": info.st_size, "sha256": digest, "mode": info.st_mode & 0o7777}


def _same_artifact(path: Path, expected: dict[str, Any] | None) -> bool:
    if expected is None:
        return not path.exists()
    try:
        actual = _hash_file(path)
    except OSError:
        return False
    return actual["size"] == expected.get("size") and actual["sha256"] == expected.get("sha256")


def _safe_base_name(collection: str, volume_id: int) -> str:
    base_name = f"{collection}_{volume_id}" if collection else str(volume_id)
    if not base_name or base_name in {".", ".."} or Path(base_name).name != base_name or "\x00" in base_name:
        raise IndexRepairError(f"unsafe collection-derived volume filename for volume {volume_id}")
    return base_name


class RepairJournal:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.journal_dir = root / "journal"
        self.staging_dir = root / "staging"
        self.backup_dir = root / "backups"
        self.lock_path = root / "journal.lock"

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(self.root, 0, 0)
        os.chmod(self.root, 0o711)
        for path, mode in (
            (self.journal_dir, 0o700), (self.staging_dir, 0o711), (self.backup_dir, 0o700),
        ):
            path.mkdir(parents=True, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(path, 0, 0)
            os.chmod(path, mode)
        _fsync_directory(self.root.parent)
        _fsync_directory(self.root)

    def _lock(self, *, exclusive: bool) -> Any:
        self.prepare()
        handle = self.lock_path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        return handle

    def load_all(self) -> list[dict[str, Any]]:
        handle = self._lock(exclusive=False)
        try:
            transactions: list[dict[str, Any]] = []
            for path in sorted(self.journal_dir.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise IndexRepairJournalError(f"repair journal is unreadable: {path}: {exc}") from exc
                if not isinstance(payload, dict) or payload.get("transaction_id") != path.stem:
                    raise IndexRepairJournalError(f"repair journal is structurally invalid: {path}")
                required = {"version", "transaction_id", "volume_id", "collection", "phase", "phase_history"}
                if not required.issubset(payload) or not isinstance(payload["phase_history"], list):
                    raise IndexRepairJournalError(f"repair journal is structurally invalid: {path}")
                transactions.append(payload)
            return transactions
        finally:
            handle.close()

    def load_all_readonly(self) -> list[dict[str, Any]]:
        """Read atomic journal records without creating files or taking a write lock."""

        if not self.journal_dir.exists():
            return []
        transactions: list[dict[str, Any]] = []
        for path in sorted(self.journal_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise IndexRepairJournalError(f"repair journal is unreadable: {path}: {exc}") from exc
            if not isinstance(payload, dict) or payload.get("transaction_id") != path.stem:
                raise IndexRepairJournalError(f"repair journal is structurally invalid: {path}")
            required = {"version", "transaction_id", "volume_id", "collection", "phase", "phase_history"}
            if not required.issubset(payload) or not isinstance(payload["phase_history"], list):
                raise IndexRepairJournalError(f"repair journal is structurally invalid: {path}")
            transactions.append(payload)
        return transactions

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction = dict(payload)
        now = time.time()
        transaction.setdefault("version", 1)
        transaction.setdefault("transaction_id", uuid.uuid4().hex)
        transaction.setdefault("phase", "detected")
        transaction.setdefault("phase_history", [{"phase": "detected", "at": now}])
        transaction.setdefault("created_at", now)
        transaction["updated_at"] = now
        self.save(transaction)
        return transaction

    def save(self, transaction: dict[str, Any]) -> None:
        handle = self._lock(exclusive=True)
        try:
            _write_json_atomic(self.journal_dir / f"{transaction['transaction_id']}.json", transaction)
        finally:
            handle.close()

    def transition(self, transaction: dict[str, Any], phase: str, **values: Any) -> None:
        now = time.time()
        transaction.update(values)
        transaction["phase"] = phase
        transaction["updated_at"] = now
        transaction.setdefault("phase_history", []).append({"phase": phase, "at": now})
        self.save(transaction)
        event(
            "info", "index_repair_phase",
            generation=transaction.get("generation_id", 0),
            transport=transaction.get("active_transport", ""),
            collection=transaction.get("collection", ""),
            volume_id=transaction.get("volume_id", 0),
            transaction_id=transaction.get("transaction_id", ""),
            source_fingerprint=transaction.get("source_fingerprint", {}),
            old_index_size=(transaction.get("old_idx") or {}).get("size"),
            candidate_index_size=(transaction.get("candidate_idx") or {}).get("size"),
            result=phase,
        )

    def request_retry(self, volume_id: int) -> dict[str, Any]:
        transactions = [item for item in self.load_all() if int(item.get("volume_id", 0)) == volume_id]
        blocked = [item for item in transactions if item.get("phase") == "manual_intervention_required"]
        if not blocked:
            raise IndexRepairError(f"volume {volume_id} has no repair failure requiring an operator retry")
        transaction = max(blocked, key=lambda item: float(item.get("updated_at", 0)))
        transaction["operator_retry_requested_at"] = time.time()
        transaction["operator_retry_consumed"] = False
        self.save(transaction)
        return transaction


class IndexRepairController:
    def __init__(
        self,
        config: Any,
        health: Any,
        command_builder: Callable[[list[str]], list[str]] | None = None,
    ) -> None:
        self.config = config
        self.health = health
        self.command_builder = command_builder or (lambda command: list(command))
        self.journal = RepairJournal(config.index_repair_path)
        self._current_volume_id = 0
        self._active_volume_ids: set[int] = set()
        self._active_lock = threading.Lock()
        self._publish("idle")

    @property
    def enabled(self) -> bool:
        return bool(self.config.seaweed.auto_index_repair_enabled)

    def _transactions(self) -> list[dict[str, Any]]:
        return self.journal.load_all()

    def awaiting(self) -> list[dict[str, Any]]:
        return [item for item in self._transactions() if item.get("phase") in PENDING_VALIDATION_PHASES]

    def has_awaiting_validation(self) -> bool:
        return bool(self.awaiting())

    def _publish(self, state: str, *, error: str = "") -> None:
        try:
            transactions = self._transactions() if self.journal.root.exists() else []
        except IndexRepairJournalError as exc:
            transactions = []
            state = "journal_corrupt"
            error = str(exc)
        pending = sorted({
            int(item["volume_id"]) for item in transactions
            if item.get("phase") not in TERMINAL_PHASES
        })
        verified = sorted({int(item["volume_id"]) for item in transactions if item.get("phase") == "verified"})
        resolved = sorted({
            int(item["volume_id"]) for item in transactions
            if item.get("phase") == "resolved_without_install"
        })
        failed = sorted({
            int(item["volume_id"]) for item in transactions
            if item.get("phase") == "manual_intervention_required"
        })
        latest = max(transactions, key=lambda item: float(item.get("updated_at", 0)), default={})
        latest_failure = max(
            (
                item for item in transactions
                if item.get("failure_reason") and item.get("phase") != "resolved_without_install"
            ),
            key=lambda item: float(item.get("updated_at", 0)),
            default={},
        )
        counters = {
            "detected_total": len(transactions),
            "attempted_total": sum(1 for item in transactions if int(item.get("attempt_count", 0)) > 0),
            "succeeded_total": sum(1 for item in transactions if item.get("phase") == "verified"),
            "resolved_without_install_total": sum(
                1 for item in transactions if item.get("phase") == "resolved_without_install"
            ),
            "failed_total": sum(1 for item in transactions if item.get("phase") == "manual_intervention_required"),
            "rolled_back_total": sum(1 for item in transactions if item.get("rolled_back")),
        }
        self.health.set_index_repair({
            "enabled": self.enabled,
            "state": state,
            "transaction_id": str(latest.get("transaction_id", "")),
            "pending_volume_ids": pending,
            "current_volume_id": self._current_volume_id,
            "verified_volume_ids": verified,
            "resolved_without_install_volume_ids": resolved,
            "failed_volume_ids": failed,
            "last_error": error or str(latest_failure.get("failure_reason", "")),
            "started_at": float(latest.get("created_at", 0)),
            "updated_at": float(latest.get("updated_at", 0)),
            "counters": counters,
        })

    def prepare_offline(self) -> None:
        self.journal.prepare()
        transactions = self._transactions()
        for transaction in transactions:
            phase = str(transaction.get("phase"))
            if phase == "installing_candidate":
                self._recover_interrupted_install(transaction)
            elif phase in PENDING_VALIDATION_PHASES:
                self._validate_installed_artifacts(transaction)
            elif phase in {"rolled_back"}:
                self.journal.transition(
                    transaction, "manual_intervention_required",
                    failure_reason=transaction.get("failure_reason", "candidate was rolled back"),
                )
            elif phase not in TERMINAL_PHASES and phase not in PREINSTALL_PHASES:
                self._manual(transaction, f"unknown crash-recovery phase: {phase}")
        self._publish("awaiting_upstream_validation" if self.has_awaiting_validation() else "idle")

    def reconcile_resolved_preinstall(self, status: dict[str, Any]) -> list[int]:
        """Close stale pre-install work after stable upstream status no longer requests it."""

        details = {int(item["id"]): item for item in status.get("volume_details", [])}
        unexpected = {int(item) for item in status.get("unexpected_readonly_volume_ids", [])}
        resolved: list[int] = []
        for transaction in self._transactions():
            if transaction.get("phase") not in PREINSTALL_PHASES:
                continue
            volume_id = int(transaction["volume_id"])
            if volume_id not in details or volume_id in unexpected:
                continue
            reason = "stable upstream status no longer reports this volume as unexpectedly read-only"
            self.journal.transition(
                transaction,
                "resolved_without_install",
                success=False,
                candidate_installed=False,
                resolution_reason=reason,
                resolved_at=time.time(),
            )
            expected_staging = self.journal.staging_dir / str(transaction["transaction_id"])
            staging = Path(str(transaction.get("staging_dir") or expected_staging))
            if staging == expected_staging:
                if staging.is_symlink():
                    staging.unlink(missing_ok=True)
                else:
                    shutil.rmtree(staging, ignore_errors=True)
            resolved.append(volume_id)
        if resolved:
            self._publish("idle")
        return sorted(resolved)

    def _validate_installed_artifacts(self, transaction: dict[str, Any]) -> None:
        live_idx = Path(transaction["live_idx_path"])
        if not _same_artifact(live_idx, transaction.get("candidate_idx")):
            self._manual(transaction, "installed live index does not match the journaled candidate")
            raise IndexRepairManualIntervention(
                f"volume {transaction['volume_id']} live index is inconsistent with its repair journal"
            )
        if Path(transaction["live_sdx_path"]).exists():
            self._manual(transaction, "stale .sdx exists after candidate installation")
            raise IndexRepairManualIntervention(
                f"volume {transaction['volume_id']} has a stale .sdx after repair installation"
            )
        if transaction.get("phase") != "awaiting_upstream_validation":
            self.journal.transition(transaction, "awaiting_upstream_validation")

    def _recover_interrupted_install(self, transaction: dict[str, Any]) -> None:
        live_idx = Path(transaction["live_idx_path"])
        if _same_artifact(live_idx, transaction.get("candidate_idx")):
            self._remove_stale_sdx(transaction)
            self.journal.transition(transaction, "candidate_installed")
            self.journal.transition(transaction, "awaiting_upstream_validation")
            return
        if _same_artifact(live_idx, transaction.get("old_idx")):
            temporary = Path(str(transaction.get("install_temp_path", "")))
            if temporary.is_file() and _same_artifact(temporary, transaction.get("candidate_idx")):
                os.replace(temporary, live_idx)
                self._remove_stale_sdx(transaction)
                _fsync_directory(live_idx.parent)
                self.journal.transition(transaction, "candidate_installed")
                self.journal.transition(transaction, "awaiting_upstream_validation")
                return
            temporary.unlink(missing_ok=True)
            self._install_candidate(transaction)
            return
        self._manual(transaction, "interrupted install left an unrecognized live index")
        raise IndexRepairManualIntervention(
            f"volume {transaction['volume_id']} interrupted install cannot be reconciled safely"
        )

    def _run_helper(
        self,
        *,
        source: Path,
        staging: Path,
        base_name: str,
        collection: str,
        volume_id: int,
        fingerprint_only: bool,
    ) -> dict[str, Any]:
        command = [
            "unshare", "--mount", "--propagation", "private", "--",
            sys.executable, "-m", "s3_storage_node.index_repair_helper",
            "--source-dat", str(source),
            "--staging-dir", str(staging),
            "--base-name", base_name,
            "--volume-id", str(volume_id),
            "--weed-binary", self.config.seaweed.binary,
            "--uid", str(self.config.appliance.uid),
            "--gid", str(self.config.appliance.gid),
        ]
        if collection:
            command.extend(["--collection", collection])
        if fingerprint_only:
            command.append("--fingerprint-only")
        process = subprocess.Popen(
            self.command_builder(command), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=self.config.seaweed.index_repair_timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise IndexRepairError(
                f"index repair helper timed out after {self.config.seaweed.index_repair_timeout_seconds} seconds"
            ) from exc
        lines = [line for line in stdout.splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError as exc:
            raise IndexRepairError(f"index repair helper returned invalid output: {stderr[-1024:]}") from exc
        if not isinstance(payload, dict):
            raise IndexRepairError("index repair helper returned an invalid result")
        if process.returncode != 0 or not payload.get("success"):
            raise IndexRepairError(str(payload.get("error") or stderr.strip() or "index repair helper failed"))
        return payload

    @staticmethod
    def _enrich_fingerprint(
        raw: dict[str, Any], *, dataset: str, transport: str, collection: str, volume_id: int,
    ) -> dict[str, Any]:
        return {
            "dataset_sentinel_id": dataset,
            "active_transport": transport,
            "collection": collection,
            "volume_id": volume_id,
            **raw,
        }

    def repair_detected(
        self,
        status: dict[str, Any],
        *,
        generation_id: int,
        active_transport: str,
        generation_failure_cause: str,
    ) -> None:
        if not self.enabled:
            raise IndexRepairManualIntervention("automatic SeaweedFS index repair is disabled")
        self.health.set("REPAIRING_INDEXES", False, "rebuilding indexes from read-only authoritative .dat files")
        details = {
            int(item["id"]): item for item in status.get("volume_details", [])
            if bool(item.get("readonly")) and not bool(item.get("expected_readonly"))
        }
        unexpected = [int(item) for item in status.get("unexpected_readonly_volume_ids", [])]
        if set(details) != set(unexpected):
            raise IndexRepairError("upstream volume status did not provide unambiguous repair metadata")
        failures: list[str] = []
        def repair_volume(volume_id: int) -> None:
            with self._active_lock:
                self._active_volume_ids.add(volume_id)
                self._current_volume_id = min(self._active_volume_ids)
            self._publish("repairing")
            try:
                self._repair_one(
                    details[volume_id], generation_id=generation_id, active_transport=active_transport,
                    generation_failure_cause=generation_failure_cause,
                )
            finally:
                with self._active_lock:
                    self._active_volume_ids.discard(volume_id)
                    self._current_volume_id = min(self._active_volume_ids, default=0)

        concurrency = min(int(self.config.seaweed.index_repair_concurrency), len(unexpected))
        if concurrency <= 1:
            work = ((volume_id, None) for volume_id in unexpected)
            for volume_id, _unused in work:
                try:
                    repair_volume(volume_id)
                except IndexRepairError as exc:
                    failures.append(f"volume {volume_id}: {exc}")
                    event("error", "index_repair_volume_failed", volume_id=volume_id, error=str(exc))
        else:
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="index-repair") as executor:
                futures = {executor.submit(repair_volume, volume_id): volume_id for volume_id in unexpected}
                for future in as_completed(futures):
                    volume_id = futures[future]
                    try:
                        future.result()
                    except IndexRepairError as exc:
                        failures.append(f"volume {volume_id}: {exc}")
                        event("error", "index_repair_volume_failed", volume_id=volume_id, error=str(exc))
        self._current_volume_id = 0
        self._publish("awaiting_upstream_validation" if self.has_awaiting_validation() else "failed")
        if failures and not self.has_awaiting_validation():
            raise IndexRepairManualIntervention("; ".join(failures))

    def _repair_one(
        self,
        detail: dict[str, Any],
        *,
        generation_id: int,
        active_transport: str,
        generation_failure_cause: str,
    ) -> None:
        volume_id = int(detail["id"])
        collection = str(detail.get("collection") or "")
        base_name = _safe_base_name(collection, volume_id)
        source = self.config.volume_path / f"{base_name}.dat"
        source_identity = {
            "dataset_sentinel_id": self.config.data_target.sentinel_id,
            "active_transport": active_transport,
            "collection": collection,
            "volume_id": volume_id,
        }
        unfingerprinted_retry = self._authorize_unfingerprinted_retry(source_identity)
        preflight_dir = self.journal.staging_dir / f"preflight-{uuid.uuid4().hex}"
        try:
            preflight = self._run_helper(
                source=source, staging=preflight_dir, base_name=base_name, collection=collection,
                volume_id=volume_id, fingerprint_only=True,
            )
            source_fingerprint = self._enrich_fingerprint(
                preflight["source_fingerprint_before"],
                dataset=self.config.data_target.sentinel_id,
                transport=active_transport,
                collection=collection,
                volume_id=volume_id,
            )
        except (IndexRepairError, KeyError, TypeError, ValueError) as exc:
            reason = f"source preflight failed: {exc}"
            self._record_unfingerprinted_failure(
                source_identity=source_identity,
                source=source,
                base_name=base_name,
                generation_id=generation_id,
                generation_failure_cause=generation_failure_cause,
                reason=reason,
                retry_authorized=unfingerprinted_retry,
            )
            raise IndexRepairError(reason) from exc
        finally:
            shutil.rmtree(preflight_dir, ignore_errors=True)
        same_source = [
            item for item in self._transactions()
            if item.get("source_fingerprint") == source_fingerprint
        ]
        verified = [item for item in same_source if item.get("phase") == "verified"]
        if verified:
            raise IndexRepairManualIntervention(
                "this authoritative .dat fingerprint was already repaired and verified; refusing a repair loop"
            )
        blocked = [item for item in same_source if item.get("phase") == "manual_intervention_required"]
        retry_authorized = False
        if blocked:
            latest = max(blocked, key=lambda item: float(item.get("updated_at", 0)))
            retry_authorized = bool(
                latest.get("operator_retry_requested_at") and not latest.get("operator_retry_consumed")
            )
            if not retry_authorized:
                raise IndexRepairManualIntervention(
                    "this authoritative .dat fingerprint requires explicit operator retry"
                )
            latest["operator_retry_consumed"] = True
            self.journal.save(latest)

        attempts = max(
            (
                int(item.get("attempt_count", 0)) for item in same_source
                if item.get("phase") != "resolved_without_install"
            ),
            default=0,
        )
        if attempts >= PREINSTALL_RETRY_LIMIT and not retry_authorized:
            failed = [item for item in same_source if item.get("phase") == "failed_preinstall"]
            if failed:
                latest = max(failed, key=lambda item: float(item.get("updated_at", 0)))
                self._manual(latest, "bounded automatic repair attempts are exhausted for this .dat")
            raise IndexRepairManualIntervention(
                "bounded automatic repair attempts are exhausted for this .dat"
            )
        for stale in self._transactions():
            if (
                int(stale.get("volume_id", 0)) == volume_id
                and stale.get("source_fingerprint") != source_fingerprint
                and stale.get("phase") not in TERMINAL_PHASES
                and stale.get("phase") not in PENDING_VALIDATION_PHASES
            ):
                self._manual(stale, "authoritative .dat fingerprint changed before transaction resume")
        live_idx = self.config.index_path / f"{base_name}.idx"
        live_sdx = self.config.index_path / f"{base_name}.sdx"
        if not live_idx.is_file() or live_idx.is_symlink():
            reason = f"live index is missing or unsafe: {live_idx}"
            self._record_structural_failure(
                source_identity=source_identity,
                source_fingerprint=source_fingerprint,
                source=source,
                base_name=base_name,
                live_idx=live_idx,
                live_sdx=live_sdx,
                generation_id=generation_id,
                generation_failure_cause=generation_failure_cause,
                attempt_count=attempts + 1,
                reason=reason,
            )
            raise IndexRepairManualIntervention(reason)
        if live_sdx.is_symlink():
            reason = f"live sorted index is unsafe: {live_sdx}"
            self._record_structural_failure(
                source_identity=source_identity,
                source_fingerprint=source_fingerprint,
                source=source,
                base_name=base_name,
                live_idx=live_idx,
                live_sdx=live_sdx,
                generation_id=generation_id,
                generation_failure_cause=generation_failure_cause,
                attempt_count=attempts + 1,
                reason=reason,
            )
            raise IndexRepairManualIntervention(reason)
        old_idx = _hash_file(live_idx)
        old_sdx = _hash_file(live_sdx) if live_sdx.is_file() else None
        resumable = [
            item for item in same_source
            if item.get("phase") in PREINSTALL_PHASES - {"failed_preinstall"}
        ]
        if resumable:
            transaction = max(resumable, key=lambda item: float(item.get("updated_at", 0)))
            if transaction.get("old_idx") != old_idx or transaction.get("old_sdx") != old_sdx:
                self._manual(transaction, "live derived state changed while a repair transaction was pending")
                raise IndexRepairManualIntervention("live index state changed during crash recovery")
            if transaction.get("phase") == "backup_created":
                self._install_candidate(transaction)
                return
            if transaction.get("phase") == "candidate_built":
                self._create_backups(transaction)
                self._install_candidate(transaction)
                return
            staging = Path(str(transaction.get("staging_dir") or (
                self.journal.staging_dir / transaction["transaction_id"]
            )))
            (staging / f"{base_name}.idx").unlink(missing_ok=True)
        else:
            transaction = self.journal.create({
                "generation_id": generation_id,
                "active_transport": active_transport,
                "generation_failure_cause": generation_failure_cause,
                "dataset_sentinel_id": self.config.data_target.sentinel_id,
                "source_identity": source_identity,
                "collection": collection,
                "volume_id": volume_id,
                "base_name": base_name,
                "source_dat_path": str(source),
                "source_fingerprint": source_fingerprint,
                "live_idx_path": str(live_idx),
                "live_sdx_path": str(live_sdx),
                "old_idx": old_idx,
                "old_sdx": old_sdx,
                "attempt_count": attempts + 1,
            })
            staging = self.journal.staging_dir / transaction["transaction_id"]
        self.journal.transition(transaction, "writers_stopped")
        transaction["staging_dir"] = str(staging)
        self.journal.transition(transaction, "source_mounted_readonly")
        self.journal.transition(transaction, "candidate_building")
        try:
            built = self._run_helper(
                source=source, staging=staging, base_name=base_name, collection=collection,
                volume_id=volume_id, fingerprint_only=False,
            )
            built_fingerprint = self._enrich_fingerprint(
                built["source_fingerprint_after"],
                dataset=self.config.data_target.sentinel_id,
                transport=active_transport,
                collection=collection,
                volume_id=volume_id,
            )
            if built_fingerprint != source_fingerprint:
                candidate_value = built.get("candidate")
                if candidate_value:
                    Path(str(candidate_value)).unlink(missing_ok=True)
                raise IndexRepairError(
                    "authoritative .dat changed between preflight and candidate reconstruction"
                )
            transaction["readonly_mount_verified"] = bool(built.get("readonly_mount_verified"))
            transaction["write_rejected"] = bool(built.get("write_rejected"))
            if not transaction["readonly_mount_verified"] or not transaction["write_rejected"]:
                raise IndexRepairError("read-only source exposure was not proven")
            candidate = Path(built["candidate"])
            transaction["candidate_path"] = str(candidate)
            transaction["candidate_idx"] = {
                "size": int(built["candidate_size"]),
                "sha256": str(built["candidate_sha256"]),
            }
        except (IndexRepairError, KeyError, OSError, TypeError, ValueError) as exc:
            reason = str(exc)
            self._record_preinstall_failure(transaction, reason)
            raise IndexRepairError(reason) from exc
        self.journal.transition(transaction, "candidate_built")
        self._create_backups(transaction)
        self._install_candidate(transaction)

    def _record_preinstall_failure(self, transaction: dict[str, Any], reason: str) -> None:
        self.journal.transition(transaction, "failed_preinstall", failure_reason=reason)
        if int(transaction.get("attempt_count", 0)) >= PREINSTALL_RETRY_LIMIT:
            self._manual(transaction, reason)

    def _record_unfingerprinted_failure(
        self,
        *,
        source_identity: dict[str, Any],
        source: Path,
        base_name: str,
        generation_id: int,
        generation_failure_cause: str,
        reason: str,
        retry_authorized: bool,
    ) -> None:
        related = [
            item for item in self._transactions()
            if item.get("source_identity") == source_identity and item.get("fingerprint_unavailable")
        ]
        attempts = max((int(item.get("attempt_count", 0)) for item in related), default=0)
        if attempts >= PREINSTALL_RETRY_LIMIT and not retry_authorized:
            raise IndexRepairManualIntervention(
                "bounded automatic source preflight attempts are exhausted"
            )
        transaction = self.journal.create({
            "generation_id": generation_id,
            "active_transport": source_identity["active_transport"],
            "generation_failure_cause": generation_failure_cause,
            "dataset_sentinel_id": source_identity["dataset_sentinel_id"],
            "source_identity": source_identity,
            "fingerprint_unavailable": True,
            "collection": source_identity["collection"],
            "volume_id": source_identity["volume_id"],
            "base_name": base_name,
            "source_dat_path": str(source),
            "source_fingerprint": {**source_identity, "unavailable": True},
            "attempt_count": attempts + 1,
        })
        self._record_preinstall_failure(transaction, reason)

    def _authorize_unfingerprinted_retry(self, source_identity: dict[str, Any]) -> bool:
        related = [
            item for item in self._transactions()
            if item.get("source_identity") == source_identity and item.get("fingerprint_unavailable")
        ]
        blocked = [item for item in related if item.get("phase") == "manual_intervention_required"]
        if not blocked:
            return False
        latest = max(blocked, key=lambda item: float(item.get("updated_at", 0)))
        authorized = bool(
            latest.get("operator_retry_requested_at") and not latest.get("operator_retry_consumed")
        )
        if not authorized:
            raise IndexRepairManualIntervention(
                "source preflight failures require explicit operator retry"
            )
        latest["operator_retry_consumed"] = True
        self.journal.save(latest)
        return True

    def _record_structural_failure(
        self,
        *,
        source_identity: dict[str, Any],
        source_fingerprint: dict[str, Any],
        source: Path,
        base_name: str,
        live_idx: Path,
        live_sdx: Path,
        generation_id: int,
        generation_failure_cause: str,
        attempt_count: int,
        reason: str,
    ) -> None:
        transaction = self.journal.create({
            "generation_id": generation_id,
            "active_transport": source_identity["active_transport"],
            "generation_failure_cause": generation_failure_cause,
            "dataset_sentinel_id": source_identity["dataset_sentinel_id"],
            "source_identity": source_identity,
            "collection": source_identity["collection"],
            "volume_id": source_identity["volume_id"],
            "base_name": base_name,
            "source_dat_path": str(source),
            "source_fingerprint": source_fingerprint,
            "live_idx_path": str(live_idx),
            "live_sdx_path": str(live_sdx),
            "old_idx": None,
            "old_sdx": None,
            "attempt_count": attempt_count,
        })
        self._manual(transaction, reason)

    def _backup_one(self, source: Path, destination: Path, expected: dict[str, Any]) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not _same_artifact(destination, expected):
                raise IndexRepairError(f"existing repair backup has the wrong hash: {destination}")
            return
        try:
            os.link(source, destination)
        except OSError:
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.copying")
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            shutil.copystat(source, temporary, follow_symlinks=False)
            with temporary.open("rb") as copied:
                os.fsync(copied.fileno())
            os.replace(temporary, destination)
        if not _same_artifact(destination, expected):
            raise IndexRepairError(f"repair backup verification failed: {destination}")
        _fsync_directory(destination.parent)

    def _create_backups(self, transaction: dict[str, Any]) -> None:
        backup = self.journal.backup_dir / transaction["transaction_id"]
        backup.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(backup, 0, 0)
        os.chmod(backup, 0o700)
        idx_backup = backup / f"{transaction['base_name']}.idx"
        self._backup_one(Path(transaction["live_idx_path"]), idx_backup, transaction["old_idx"])
        transaction["backup_idx_path"] = str(idx_backup)
        if transaction.get("old_sdx") is not None:
            sdx_backup = backup / f"{transaction['base_name']}.sdx"
            self._backup_one(Path(transaction["live_sdx_path"]), sdx_backup, transaction["old_sdx"])
            transaction["backup_sdx_path"] = str(sdx_backup)
        _fsync_directory(backup)
        self.journal.transition(transaction, "backup_created")

    def _install_candidate(self, transaction: dict[str, Any]) -> None:
        candidate = Path(transaction["candidate_path"])
        if not _same_artifact(candidate, transaction["candidate_idx"]):
            self._manual(transaction, "candidate artifact no longer matches its journaled hash")
            raise IndexRepairManualIntervention("candidate artifact is missing or changed")
        live_idx = Path(transaction["live_idx_path"])
        temporary = live_idx.with_name(f".{live_idx.name}.{transaction['transaction_id']}.installing")
        if temporary.exists() and not _same_artifact(temporary, transaction["candidate_idx"]):
            temporary.unlink()
        if not temporary.exists():
            with candidate.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                os.fchmod(writer.fileno(), int(transaction["old_idx"].get("mode", 0o644)))
                os.fchown(writer.fileno(), self.config.appliance.uid, self.config.appliance.gid)
                writer.flush()
                os.fsync(writer.fileno())
        transaction["install_temp_path"] = str(temporary)
        self.journal.transition(transaction, "installing_candidate")
        os.replace(temporary, live_idx)
        self._remove_stale_sdx(transaction)
        _fsync_directory(live_idx.parent)
        if not _same_artifact(live_idx, transaction["candidate_idx"]):
            raise IndexRepairError("live index does not match candidate after atomic installation")
        self.journal.transition(transaction, "candidate_installed")
        self.journal.transition(transaction, "awaiting_upstream_validation")

    def _remove_stale_sdx(self, transaction: dict[str, Any]) -> None:
        live_sdx = Path(transaction["live_sdx_path"])
        live_sdx.unlink(missing_ok=True)
        _fsync_directory(live_sdx.parent)

    def validate_upstream(self, status: dict[str, Any]) -> tuple[list[int], list[int]]:
        details = {int(item["id"]): item for item in status.get("volume_details", [])}
        verified: list[int] = []
        rejected: list[int] = []
        for transaction in self.awaiting():
            volume_id = int(transaction["volume_id"])
            upstream = details.get(volume_id)
            if upstream is None or bool(upstream.get("readonly")):
                rejected.append(volume_id)
            else:
                self.journal.transition(transaction, "verified", success=True, verified_at=time.time())
                verified.append(volume_id)
        self._publish("rollback_required" if rejected else "verified")
        return verified, rejected

    def rollback_rejected(self, volume_ids: list[int], reason: str) -> None:
        targets = [item for item in self.awaiting() if int(item["volume_id"]) in set(volume_ids)]
        for transaction in targets:
            self._rollback(transaction, reason)
        self._publish("manual_intervention_required", error=reason)

    def _restore_one(self, backup: Path, destination: Path, expected: dict[str, Any]) -> None:
        if not _same_artifact(backup, expected):
            raise IndexRepairManualIntervention(f"repair backup is missing or changed: {backup}")
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.restoring")
        with backup.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
            os.fchmod(writer.fileno(), int(expected.get("mode", 0o644)))
            os.fchown(writer.fileno(), self.config.appliance.uid, self.config.appliance.gid)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)

    def _rollback(self, transaction: dict[str, Any], reason: str) -> None:
        self._restore_one(
            Path(transaction["backup_idx_path"]), Path(transaction["live_idx_path"]), transaction["old_idx"],
        )
        live_sdx = Path(transaction["live_sdx_path"])
        if transaction.get("old_sdx") is not None:
            self._restore_one(Path(transaction["backup_sdx_path"]), live_sdx, transaction["old_sdx"])
        else:
            live_sdx.unlink(missing_ok=True)
        _fsync_directory(Path(transaction["live_idx_path"]).parent)
        self.journal.transition(transaction, "rolled_back", rolled_back=True, failure_reason=reason)
        self._manual(transaction, reason)

    def _manual(self, transaction: dict[str, Any], reason: str) -> None:
        self.journal.transition(
            transaction, "manual_intervention_required", success=False, failure_reason=reason,
        )

    def unresolved_unexpected(self, status: dict[str, Any]) -> list[int]:
        unexpected = {int(item) for item in status.get("unexpected_readonly_volume_ids", [])}
        awaiting = {int(item["volume_id"]) for item in self.awaiting()}
        return sorted(unexpected - awaiting)


def journal_status(config: Any) -> dict[str, Any]:
    journal = RepairJournal(config.index_repair_path)
    transactions = journal.load_all_readonly()
    return {
        "enabled": bool(config.seaweed.auto_index_repair_enabled),
        "repair_root": str(config.index_repair_path),
        "transactions": transactions,
    }
