from __future__ import annotations

import pytest

from s3_storage_node.seaweed_health import SeaweedHealthError, all_volumes_readonly, parse_volume_status


def test_volume_status_uses_upstream_readonly_state_and_reports_collections() -> None:
    result = parse_volume_status(
        {
            "Volumes": [
                {"Id": 23, "Collection": "s3-orchestrator-data", "ReadOnly": True},
                {"Id": 24, "Collection": "s3-orchestrator-data", "ReadOnly": False},
                {"Id": 99, "Collection": "archive", "ReadOnly": True},
            ]
        },
        expected_readonly_ids={99},
    )

    assert result["total"] == 3
    assert result["readonly"] == 2
    assert result["writable"] == 1
    assert result["unexpected_readonly_volume_ids"] == [23]
    assert result["volume_details"] == [
        {"id": 23, "collection": "s3-orchestrator-data", "readonly": True, "expected_readonly": False},
        {"id": 24, "collection": "s3-orchestrator-data", "readonly": False, "expected_readonly": False},
        {"id": 99, "collection": "archive", "readonly": True, "expected_readonly": True},
    ]
    assert result["collections"]["s3-orchestrator-data"] == {
        "total": 2, "readonly": 1, "writable": 1,
    }


def test_expected_readonly_ids_do_not_duplicate_seaweed_lifecycle_rules() -> None:
    result = parse_volume_status(
        {"Volumes": [{"Id": 64, "Collection": "historical", "ReadOnly": True}]},
        expected_readonly_ids={64},
    )
    assert result["unexpected_readonly"] == 0
    assert result["expected_readonly"] == 1


def test_global_readonly_state_is_identified_without_reclassifying_individual_volumes() -> None:
    result = parse_volume_status(
        {
            "Volumes": [
                {"Id": 1, "ReadOnly": True},
                {"Id": 2, "ReadOnly": True},
                {"Id": 3, "ReadOnly": True},
            ]
        },
        expected_readonly_ids={3},
    )

    assert all_volumes_readonly(result) is True
    assert result["unexpected_readonly_volume_ids"] == [1, 2]
    assert all_volumes_readonly(parse_volume_status(
        {"Volumes": [{"Id": 1, "ReadOnly": False}, {"Id": 2, "ReadOnly": True}]},
        expected_readonly_ids=set(),
    )) is False


def test_nil_upstream_volume_list_is_an_empty_server() -> None:
    result = parse_volume_status({"Volumes": None}, expected_readonly_ids=set())
    assert result["total"] == 0
    assert result["unexpected_readonly"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"Volumes": "invalid"},
        {"Volumes": [{"Id": 1, "ReadOnly": "true"}]},
        {"Volumes": [{"ReadOnly": False}]},
    ],
)
def test_malformed_upstream_status_fails_closed(payload: object) -> None:
    with pytest.raises(SeaweedHealthError):
        parse_volume_status(payload, expected_readonly_ids=set())
