from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from s3_storage_node.health import Handler, HealthState
from s3_storage_node.index_repair import (
    IndexRepairController,
    IndexRepairError,
    IndexRepairJournalError,
    IndexRepairManualIntervention,
    RepairJournal,
)


def make_config(tmp_path: Path) -> SimpleNamespace:
    index_path = tmp_path / "index"
    volume_path = tmp_path / "volumes"
    index_path.mkdir()
    volume_path.mkdir()
    return SimpleNamespace(
        index_path=index_path,
        index_repair_path=index_path / ".s3-storage-node-repair",
        volume_path=volume_path,
        data_target=SimpleNamespace(sentinel_id="dataset-1"),
        appliance=SimpleNamespace(uid=os.getuid(), gid=os.getgid()),
        seaweed=SimpleNamespace(
            auto_index_repair_enabled=True,
            binary="/usr/local/bin/weed",
            index_repair_concurrency=1,
            index_repair_timeout_seconds=30,
        ),
    )


def status(readonly: bool = True) -> dict[str, object]:
    return {
        "unexpected_readonly_volume_ids": [1] if readonly else [],
        "volume_details": [{
            "id": 1,
            "collection": "photos",
            "readonly": readonly,
            "expected_readonly": False,
        }],
    }


def test_missing_target_is_not_mistaken_for_success(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.index_path / "photos_1.idx").write_bytes(b"old-index")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    install_fake_builder(controller)
    controller.repair_detected(
        status(), generation_id=6, active_transport="sshfs-secondary",
        generation_failure_cause="seaweed_volume_health_failure",
    )

    verified, rejected = controller.validate_upstream({
        "unexpected_readonly_volume_ids": [],
        "volume_details": [],
    })
    assert verified == []
    assert rejected == [1]


def raw_fingerprint(marker: str = "a") -> dict[str, object]:
    return {
        "size": 100,
        "mtime_ns": 123,
        "head_bytes": 100,
        "head_sha256": marker * 64,
        "tail_offset": 0,
        "tail_bytes": 100,
        "tail_sha256": marker * 64,
    }


def install_fake_builder(
    controller: IndexRepairController,
    *,
    candidate: bytes = b"candidate-index",
    build_fingerprint: dict[str, object] | None = None,
) -> None:
    source_fingerprint = raw_fingerprint()

    def run_helper(**kwargs):
        if kwargs["fingerprint_only"]:
            return {
                "success": True,
                "source_fingerprint_before": source_fingerprint,
                "source_fingerprint_after": source_fingerprint,
                "readonly_mount_verified": True,
                "write_rejected": True,
            }
        staging = Path(kwargs["staging"])
        staging.mkdir(parents=True, exist_ok=True)
        candidate_path = staging / "photos_1.idx"
        candidate_path.write_bytes(candidate)
        import hashlib

        return {
            "success": True,
            "candidate": str(candidate_path),
            "candidate_size": len(candidate),
            "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
            "source_fingerprint_before": source_fingerprint,
            "source_fingerprint_after": build_fingerprint or source_fingerprint,
            "readonly_mount_verified": True,
            "write_rejected": True,
        }

    controller._run_helper = run_helper  # type: ignore[method-assign]


def test_atomic_install_retains_idx_and_sdx_backups(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    live_idx = config.index_path / "photos_1.idx"
    live_sdx = config.index_path / "photos_1.sdx"
    live_idx.write_bytes(b"old-index")
    live_sdx.write_bytes(b"stale-sorted-index")
    health = HealthState()
    controller = IndexRepairController(config, health)
    controller.prepare_offline()
    install_fake_builder(controller)

    controller.repair_detected(
        status(), generation_id=7, active_transport="sshfs-secondary",
        generation_failure_cause="seaweed_volume_health_failure",
    )

    assert live_idx.read_bytes() == b"candidate-index"
    assert not live_sdx.exists()
    transaction = controller.awaiting()[0]
    assert Path(transaction["backup_idx_path"]).read_bytes() == b"old-index"
    assert Path(transaction["backup_sdx_path"]).read_bytes() == b"stale-sorted-index"
    assert transaction["readonly_mount_verified"] is True
    assert transaction["write_rejected"] is True
    assert transaction["source_fingerprint"]["dataset_sentinel_id"] == "dataset-1"


def test_upstream_rejection_rolls_back_idx_and_sdx(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    live_idx = config.index_path / "photos_1.idx"
    live_sdx = config.index_path / "photos_1.sdx"
    live_idx.write_bytes(b"old-index")
    live_sdx.write_bytes(b"old-sdx")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    install_fake_builder(controller)
    controller.repair_detected(
        status(), generation_id=8, active_transport="cifs-primary",
        generation_failure_cause="seaweed_volume_health_failure",
    )

    verified, rejected = controller.validate_upstream(status(readonly=True))
    assert verified == []
    assert rejected == [1]
    controller.rollback_rejected(rejected, "upstream kept volume read-only")

    assert live_idx.read_bytes() == b"old-index"
    assert live_sdx.read_bytes() == b"old-sdx"
    transaction = controller.journal.load_all()[0]
    assert transaction["phase"] == "manual_intervention_required"
    assert transaction["rolled_back"] is True


def test_changed_source_aborts_before_live_install(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    live_idx = config.index_path / "photos_1.idx"
    live_idx.write_bytes(b"old-index")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    install_fake_builder(controller, build_fingerprint=raw_fingerprint("b"))

    with pytest.raises(IndexRepairManualIntervention, match="volume 1"):
        controller.repair_detected(
            status(), generation_id=9, active_transport="sshfs-secondary",
            generation_failure_cause="seaweed_volume_health_failure",
        )

    assert live_idx.read_bytes() == b"old-index"
    transaction = controller.journal.load_all()[0]
    assert transaction["phase"] == "failed_preinstall"
    assert not list(controller.journal.backup_dir.iterdir())


def test_source_preflight_failures_are_bounded_and_require_operator_retry(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.index_path / "photos_1.idx").write_bytes(b"old-index")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    calls = 0

    def failing_preflight(**_kwargs):
        nonlocal calls
        calls += 1
        raise IndexRepairError("transport unavailable")

    controller._run_helper = failing_preflight  # type: ignore[method-assign]
    for _attempt in range(2):
        with pytest.raises(IndexRepairManualIntervention):
            controller.repair_detected(
                status(), generation_id=9, active_transport="sshfs-secondary",
                generation_failure_cause="seaweed_volume_health_failure",
            )
    assert calls == 2
    assert any(
        item["phase"] == "manual_intervention_required"
        for item in controller.journal.load_all()
    )

    with pytest.raises(IndexRepairManualIntervention, match="explicit operator retry"):
        controller.repair_detected(
            status(), generation_id=10, active_transport="sshfs-secondary",
            generation_failure_cause="seaweed_volume_health_failure",
        )
    assert calls == 2

    controller.journal.request_retry(1)
    with pytest.raises(IndexRepairManualIntervention):
        controller.repair_detected(
            status(), generation_id=11, active_transport="sshfs-secondary",
            generation_failure_cause="seaweed_volume_health_failure",
        )
    assert calls == 3


def test_missing_live_index_is_persistently_rejected(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    install_fake_builder(controller)

    with pytest.raises(IndexRepairManualIntervention, match="volume 1"):
        controller.repair_detected(
            status(), generation_id=9, active_transport="sshfs-secondary",
            generation_failure_cause="seaweed_volume_health_failure",
        )

    transactions = controller.journal.load_all()
    assert len(transactions) == 1
    assert transactions[0]["phase"] == "manual_intervention_required"
    assert "missing or unsafe" in transactions[0]["failure_reason"]

    with pytest.raises(IndexRepairManualIntervention, match="explicit operator retry"):
        controller.repair_detected(
            status(), generation_id=10, active_transport="sshfs-secondary",
            generation_failure_cause="seaweed_volume_health_failure",
        )
    assert len(controller.journal.load_all()) == 1


def test_readonly_proof_failures_reach_terminal_manual_state(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (config.index_path / "photos_1.idx").write_bytes(b"old-index")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    build_calls = 0
    source_fingerprint = raw_fingerprint()

    def unproven_builder(**kwargs):
        nonlocal build_calls
        if kwargs["fingerprint_only"]:
            return {"success": True, "source_fingerprint_before": source_fingerprint}
        build_calls += 1
        return {
            "success": True,
            "source_fingerprint_after": source_fingerprint,
            "readonly_mount_verified": False,
            "write_rejected": False,
        }

    controller._run_helper = unproven_builder  # type: ignore[method-assign]
    for _attempt in range(2):
        with pytest.raises(IndexRepairManualIntervention):
            controller.repair_detected(
                status(), generation_id=12, active_transport="sshfs-secondary",
                generation_failure_cause="seaweed_volume_health_failure",
            )
    assert build_calls == 2
    assert any(
        item["phase"] == "manual_intervention_required"
        for item in controller.journal.load_all()
    )

    with pytest.raises(IndexRepairManualIntervention, match="explicit operator retry"):
        controller.repair_detected(
            status(), generation_id=13, active_transport="sshfs-secondary",
            generation_failure_cause="seaweed_volume_health_failure",
        )
    assert build_calls == 2


def test_successful_volume_is_retained_when_another_build_fails(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.seaweed.index_repair_concurrency = 2
    (config.index_path / "photos_1.idx").write_bytes(b"old-one")
    (config.index_path / "photos_2.idx").write_bytes(b"old-two")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    source_fingerprint = raw_fingerprint()

    def run_helper(**kwargs):
        if kwargs["fingerprint_only"]:
            return {
                "success": True,
                "source_fingerprint_before": source_fingerprint,
                "source_fingerprint_after": source_fingerprint,
                "readonly_mount_verified": True,
                "write_rejected": True,
            }
        if kwargs["volume_id"] == 2:
            from s3_storage_node.index_repair import IndexRepairError

            raise IndexRepairError("scanner rejected volume 2")
        staging = Path(kwargs["staging"])
        staging.mkdir(parents=True, exist_ok=True)
        candidate = staging / "photos_1.idx"
        candidate.write_bytes(b"candidate-one")
        import hashlib

        return {
            "success": True,
            "candidate": str(candidate),
            "candidate_size": candidate.stat().st_size,
            "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "source_fingerprint_before": source_fingerprint,
            "source_fingerprint_after": source_fingerprint,
            "readonly_mount_verified": True,
            "write_rejected": True,
        }

    controller._run_helper = run_helper  # type: ignore[method-assign]
    detected = {
        "unexpected_readonly_volume_ids": [1, 2],
        "volume_details": [
            {"id": 1, "collection": "photos", "readonly": True, "expected_readonly": False},
            {"id": 2, "collection": "photos", "readonly": True, "expected_readonly": False},
        ],
    }
    controller.repair_detected(
        detected, generation_id=12, active_transport="sshfs-secondary",
        generation_failure_cause="seaweed_volume_health_failure",
    )
    assert (config.index_path / "photos_1.idx").read_bytes() == b"candidate-one"
    assert (config.index_path / "photos_2.idx").read_bytes() == b"old-two"

    validation = {
        "unexpected_readonly_volume_ids": [2],
        "volume_details": [
            {"id": 1, "collection": "photos", "readonly": False, "expected_readonly": False},
            {"id": 2, "collection": "photos", "readonly": True, "expected_readonly": False},
        ],
    }
    verified, rejected = controller.validate_upstream(validation)
    assert verified == [1]
    assert rejected == []
    assert controller.unresolved_unexpected(validation) == [2]
    assert any(item["phase"] == "verified" for item in controller.journal.load_all())


def test_verified_source_fingerprint_is_not_rebuilt(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    live_idx = config.index_path / "photos_1.idx"
    live_idx.write_bytes(b"old-index")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    install_fake_builder(controller)
    controller.repair_detected(
        status(), generation_id=10, active_transport="sshfs-secondary",
        generation_failure_cause="seaweed_volume_health_failure",
    )
    controller.validate_upstream(status(readonly=False))

    with pytest.raises(IndexRepairManualIntervention, match="already repaired and verified"):
        controller.repair_detected(
            status(), generation_id=11, active_transport="sshfs-secondary",
            generation_failure_cause="seaweed_volume_health_failure",
        )
    assert len(controller.journal.load_all()) == 1


def test_corrupt_journal_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    journal = RepairJournal(config.index_repair_path)
    journal.prepare()
    (journal.journal_dir / "broken.json").write_text("not json", encoding="utf-8")
    controller = IndexRepairController(config, HealthState())

    with pytest.raises(IndexRepairJournalError, match="unreadable"):
        controller.prepare_offline()


def test_status_reader_does_not_create_repair_state(tmp_path: Path) -> None:
    root = tmp_path / "does-not-exist"
    journal = RepairJournal(root)
    assert journal.load_all_readonly() == []
    assert not root.exists()


def test_operator_retry_is_explicit_and_one_shot(tmp_path: Path) -> None:
    journal = RepairJournal(tmp_path / "repair")
    transaction = journal.create({
        "volume_id": 4,
        "collection": "archive",
        "phase": "manual_intervention_required",
        "phase_history": [{"phase": "manual_intervention_required", "at": 1.0}],
    })
    requested = journal.request_retry(4)
    assert requested["transaction_id"] == transaction["transaction_id"]
    assert requested["operator_retry_consumed"] is False


def test_health_metrics_include_repair_counters() -> None:
    health = HealthState()
    health.set_index_repair({
        "pending_volume_ids": [1, 2],
        "current_volume_id": 2,
        "counters": {
            "detected_total": 3,
            "attempted_total": 2,
            "succeeded_total": 1,
            "resolved_without_install_total": 1,
            "failed_total": 1,
            "rolled_back_total": 1,
        },
    })
    snapshot = health.snapshot()
    lines: list[bytes] = []
    handler = SimpleNamespace(
        send_response=lambda _status: None,
        send_header=lambda _name, _value: None,
        end_headers=lambda: None,
        _write=lines.append,
        _label=Handler._label,
    )
    Handler._metrics(handler, snapshot)
    metrics = lines[0].decode()
    assert "s3_storage_node_index_repairs_succeeded_total 1" in metrics
    assert "s3_storage_node_index_repairs_resolved_without_install_total 1" in metrics
    assert "s3_storage_node_index_repair_pending 2" in metrics
    assert "s3_storage_node_index_repair_current_volume 2" in metrics


def test_structurally_invalid_journal_is_rejected(tmp_path: Path) -> None:
    journal = RepairJournal(tmp_path / "repair")
    journal.prepare()
    path = journal.journal_dir / "abc.json"
    path.write_text(json.dumps({"transaction_id": "wrong"}), encoding="utf-8")
    with pytest.raises(IndexRepairJournalError, match="structurally invalid"):
        journal.load_all()


@pytest.mark.parametrize(
    "phase",
    ["detected", "writers_stopped", "source_mounted_readonly", "candidate_building"],
)
def test_crash_before_candidate_completion_resumes_safely(tmp_path: Path, phase: str) -> None:
    config = make_config(tmp_path)
    live_idx = config.index_path / "photos_1.idx"
    live_idx.write_bytes(b"old-index")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    fingerprint = controller._enrich_fingerprint(
        raw_fingerprint(), dataset="dataset-1", transport="sshfs-secondary",
        collection="photos", volume_id=1,
    )
    from s3_storage_node.index_repair import _hash_file

    transaction = controller.journal.create({
        "generation_id": 20,
        "active_transport": "sshfs-secondary",
        "generation_failure_cause": "seaweed_volume_health_failure",
        "dataset_sentinel_id": "dataset-1",
        "collection": "photos",
        "volume_id": 1,
        "base_name": "photos_1",
        "source_dat_path": str(config.volume_path / "photos_1.dat"),
        "source_fingerprint": fingerprint,
        "live_idx_path": str(live_idx),
        "live_sdx_path": str(config.index_path / "photos_1.sdx"),
        "old_idx": _hash_file(live_idx),
        "old_sdx": None,
        "attempt_count": 1,
    })
    if phase != "detected":
        controller.journal.transition(transaction, phase)
    install_fake_builder(controller)

    controller.repair_detected(
        status(), generation_id=21, active_transport="sshfs-secondary",
        generation_failure_cause="seaweed_volume_health_failure",
    )

    assert live_idx.read_bytes() == b"candidate-index"
    assert len(controller.journal.load_all()) == 1
    assert controller.journal.load_all()[0]["phase"] == "awaiting_upstream_validation"


def test_stale_preinstall_is_resolved_when_stable_upstream_no_longer_requests_it(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    live_idx = config.index_path / "photos_1.idx"
    live_idx.write_bytes(b"old-index")
    health = HealthState()
    controller = IndexRepairController(config, health)
    controller.prepare_offline()
    fingerprint = controller._enrich_fingerprint(
        raw_fingerprint(), dataset="dataset-1", transport="sshfs-secondary",
        collection="photos", volume_id=1,
    )
    from s3_storage_node.index_repair import _hash_file

    transaction = controller.journal.create({
        "generation_id": 20,
        "active_transport": "sshfs-secondary",
        "generation_failure_cause": "seaweed_volume_health_failure",
        "dataset_sentinel_id": "dataset-1",
        "collection": "photos",
        "volume_id": 1,
        "base_name": "photos_1",
        "source_dat_path": str(config.volume_path / "photos_1.dat"),
        "source_fingerprint": fingerprint,
        "live_idx_path": str(live_idx),
        "live_sdx_path": str(config.index_path / "photos_1.sdx"),
        "old_idx": _hash_file(live_idx),
        "old_sdx": None,
        "attempt_count": 1,
    })
    staging = controller.journal.staging_dir / transaction["transaction_id"]
    staging.mkdir()
    (staging / "partial.idx").write_bytes(b"partial")
    transaction["staging_dir"] = str(staging)
    controller.journal.transition(transaction, "candidate_building")

    assert controller.reconcile_resolved_preinstall(status(readonly=False)) == [1]
    resolved = controller.journal.load_all()[0]
    assert resolved["phase"] == "resolved_without_install"
    assert resolved["candidate_installed"] is False
    assert not staging.exists()
    snapshot = health.snapshot()["index_repair"]
    assert snapshot["pending_volume_ids"] == []
    assert snapshot["resolved_without_install_volume_ids"] == [1]
    assert snapshot["counters"]["resolved_without_install_total"] == 1

    install_fake_builder(controller)
    controller.repair_detected(
        status(), generation_id=21, active_transport="sshfs-secondary",
        generation_failure_cause="seaweed_volume_health_failure",
    )
    current = controller.awaiting()[0]
    assert current["attempt_count"] == 1
    assert current["transaction_id"] != resolved["transaction_id"]


def test_resolved_preinstall_reconciliation_never_skips_upstream_candidate_validation(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    (config.index_path / "photos_1.idx").write_bytes(b"old-index")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    install_fake_builder(controller)
    controller.repair_detected(
        status(), generation_id=22, active_transport="cifs-primary",
        generation_failure_cause="seaweed_volume_health_failure",
    )

    assert controller.reconcile_resolved_preinstall(status(readonly=False)) == []
    assert controller.awaiting()[0]["phase"] == "awaiting_upstream_validation"


@pytest.mark.parametrize("phase", ["candidate_built", "backup_created"])
def test_crash_after_candidate_build_does_not_rescan_source(tmp_path: Path, phase: str) -> None:
    config = make_config(tmp_path)
    live_idx = config.index_path / "photos_1.idx"
    live_idx.write_bytes(b"old-index")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    fingerprint = controller._enrich_fingerprint(
        raw_fingerprint(), dataset="dataset-1", transport="sshfs-secondary",
        collection="photos", volume_id=1,
    )
    from s3_storage_node.index_repair import _hash_file

    staging = controller.journal.staging_dir / "crash-candidate"
    staging.mkdir()
    candidate = staging / "photos_1.idx"
    candidate.write_bytes(b"candidate-index")
    transaction = controller.journal.create({
        "generation_id": 30,
        "active_transport": "sshfs-secondary",
        "generation_failure_cause": "seaweed_volume_health_failure",
        "dataset_sentinel_id": "dataset-1",
        "collection": "photos",
        "volume_id": 1,
        "base_name": "photos_1",
        "source_dat_path": str(config.volume_path / "photos_1.dat"),
        "source_fingerprint": fingerprint,
        "live_idx_path": str(live_idx),
        "live_sdx_path": str(config.index_path / "photos_1.sdx"),
        "old_idx": _hash_file(live_idx),
        "old_sdx": None,
        "attempt_count": 1,
        "staging_dir": str(staging),
        "candidate_path": str(candidate),
        "candidate_idx": _hash_file(candidate),
    })
    controller.journal.transition(transaction, "candidate_built")
    if phase == "backup_created":
        controller._create_backups(transaction)

    def preflight_only(**kwargs):
        if not kwargs["fingerprint_only"]:
            raise AssertionError("a completed journaled candidate must not be rebuilt")
        return {
            "success": True,
            "source_fingerprint_before": raw_fingerprint(),
            "source_fingerprint_after": raw_fingerprint(),
            "readonly_mount_verified": True,
            "write_rejected": True,
        }

    controller._run_helper = preflight_only  # type: ignore[method-assign]
    controller.repair_detected(
        status(), generation_id=31, active_transport="sshfs-secondary",
        generation_failure_cause="seaweed_volume_health_failure",
    )
    assert live_idx.read_bytes() == b"candidate-index"
    assert controller.journal.load_all()[0]["phase"] == "awaiting_upstream_validation"


@pytest.mark.parametrize("replacement_completed", [False, True])
def test_interrupted_atomic_install_is_reconciled_by_hash(
    tmp_path: Path, replacement_completed: bool,
) -> None:
    config = make_config(tmp_path)
    live_idx = config.index_path / "photos_1.idx"
    live_idx.write_bytes(b"old-index")
    controller = IndexRepairController(config, HealthState())
    controller.prepare_offline()
    from s3_storage_node.index_repair import _hash_file

    candidate = controller.journal.staging_dir / "candidate.idx"
    candidate.write_bytes(b"candidate-index")
    old_idx = _hash_file(live_idx)
    candidate_idx = _hash_file(candidate)
    install_temp = config.index_path / ".photos_1.idx.crash.installing"
    install_temp.write_bytes(b"candidate-index")
    if replacement_completed:
        os.replace(install_temp, live_idx)
    transaction = controller.journal.create({
        "generation_id": 40,
        "active_transport": "sshfs-secondary",
        "generation_failure_cause": "seaweed_volume_health_failure",
        "dataset_sentinel_id": "dataset-1",
        "collection": "photos",
        "volume_id": 1,
        "base_name": "photos_1",
        "source_dat_path": str(config.volume_path / "photos_1.dat"),
        "source_fingerprint": {},
        "live_idx_path": str(live_idx),
        "live_sdx_path": str(config.index_path / "photos_1.sdx"),
        "old_idx": old_idx,
        "old_sdx": None,
        "attempt_count": 1,
        "candidate_path": str(candidate),
        "candidate_idx": candidate_idx,
        "install_temp_path": str(install_temp),
    })
    controller.journal.transition(transaction, "installing_candidate")

    restarted = IndexRepairController(config, HealthState())
    restarted.prepare_offline()

    assert live_idx.read_bytes() == b"candidate-index"
    assert restarted.journal.load_all()[0]["phase"] == "awaiting_upstream_validation"
