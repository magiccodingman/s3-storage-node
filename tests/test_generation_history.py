from __future__ import annotations

import json
from pathlib import Path

from s3_storage_node.generation_history import GenerationHistory


def test_generation_history_records_causes_and_clean_outcomes(tmp_path: Path) -> None:
    history = GenerationHistory(tmp_path)
    history.start(41, transport="cifs-primary", mode="namespace")
    history.finish(
        cause="operator_transport_switch",
        reason="operator requested sshfs-secondary",
        phase="ONLINE",
        clean_shutdown=True,
        fence_verified=True,
    )

    snapshot = history.snapshot()
    assert snapshot["counters"]["generations_created_total"] == 1
    assert snapshot["counters"]["cause:operator_transport_switch"] == 1
    assert snapshot["counters"]["clean_shutdowns_total"] == 1
    assert snapshot["recent"][-1]["transport"] == "cifs-primary"
    assert snapshot["recent"][-1]["generation"] == 41


def test_active_generation_from_prior_guardian_is_marked_unclean(tmp_path: Path) -> None:
    first = GenerationHistory(tmp_path)
    first.start(700, transport="sshfs-secondary", mode="namespace")

    recovered = GenerationHistory(tmp_path)
    assert recovered.snapshot()["active"]["generation"] == 700
    assert "cause:guardian_restart_detected" not in recovered.snapshot()["counters"]
    recovered.recover_interrupted_active()
    snapshot = recovered.snapshot()
    assert snapshot["indexes_certified"] is False
    assert snapshot["counters"]["cause:guardian_restart_detected"] == 1
    assert snapshot["recent"][-1]["generation"] == 700
    assert snapshot["recent"][-1]["clean_shutdown"] is False


def test_successful_upstream_check_can_recertify_indexes(tmp_path: Path) -> None:
    history = GenerationHistory(tmp_path)
    history.start(1, transport="cifs", mode="namespace")
    history.finish(
        cause="storage_failure",
        reason="hard fence",
        phase="ONLINE",
        clean_shutdown=False,
        fence_verified=True,
    )
    assert history.snapshot()["indexes_certified"] is False

    history.certify_indexes("SeaweedFS volume status passed")
    snapshot = history.snapshot()
    assert snapshot["indexes_certified"] is True
    assert snapshot["counters"]["index_certifications_total"] == 1


def test_unreadable_history_never_defaults_to_certified(tmp_path: Path) -> None:
    (tmp_path / "generation-history.json").write_text("not-json", encoding="utf-8")
    history = GenerationHistory(tmp_path)
    assert history.snapshot()["indexes_certified"] is False
    assert "unreadable" in history.snapshot()["index_certification_reason"]


def test_structurally_invalid_history_never_defaults_to_certified(tmp_path: Path) -> None:
    (tmp_path / "generation-history.json").write_text(
        json.dumps({"indexes_certified": True, "counters": [], "history": {}}),
        encoding="utf-8",
    )

    history = GenerationHistory(tmp_path)

    snapshot = history.snapshot()
    assert snapshot["indexes_certified"] is False
    assert snapshot["counters"] == {}
    assert snapshot["recent"] == []
