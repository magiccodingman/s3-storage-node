from __future__ import annotations

from s3_storage_node.main import selected_transport_for_target


def test_environment_transport_applies_only_to_data(monkeypatch) -> None:
    monkeypatch.setenv("S3_STORAGE_NODE_TRANSPORT", "sshfs-secondary")

    assert selected_transport_for_target("data") == "sshfs-secondary"
    assert selected_transport_for_target("metadata") == ""
    assert selected_transport_for_target("index") == ""


def test_explicit_transport_is_preserved_for_operator_validation(monkeypatch) -> None:
    monkeypatch.setenv("S3_STORAGE_NODE_TRANSPORT", "sshfs-secondary")

    assert selected_transport_for_target("data", "cifs-primary") == "cifs-primary"
    assert selected_transport_for_target("index", "unexpected") == "unexpected"
