from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from s3_storage_node.health import Handler
from s3_storage_node.s3check import S3CheckError, run_canary


ROOT = Path(__file__).resolve().parents[1]


def test_seaweedfs_release_is_pinned_to_4_44() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    digest = "sha256:e67e8c385484120b78bff47ba5f4debbca47fbd27ed1a39f016f47e8baea615b"
    assert "ARG SEAWEEDFS_VERSION=4.44" in dockerfile
    assert digest in dockerfile
    assert "SEAWEEDFS_VERSION:-4.44" in compose
    assert digest in compose


def test_compose_grants_mount_cifs_required_capability() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "- SYS_ADMIN" in compose
    assert "- DAC_READ_SEARCH" in compose


def test_example_uses_default_seaweed_disk_tier() -> None:
    config = (ROOT / "config" / "config.toml.example").read_text()
    assert 'disk_type = ""' in config
    assert 'disk_type = "archive"' not in config


def test_canary_retries_transient_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def transient(*_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise S3CheckError("volume server not writable yet")

    monkeypatch.setattr("s3_storage_node.s3check._run_canary_once", transient)
    run_canary(18333, "access", "secret", retry_seconds=1, retry_interval_seconds=0)
    assert attempts == 3


def test_canary_still_fails_closed_after_retry_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    def always_fails(*_args: object, **_kwargs: object) -> None:
        raise S3CheckError("still unavailable")

    monkeypatch.setattr("s3_storage_node.s3check._run_canary_once", always_fails)
    with pytest.raises(S3CheckError, match="still unavailable"):
        run_canary(18333, "access", "secret", retry_seconds=0, retry_interval_seconds=0)


@pytest.mark.parametrize("error", [BrokenPipeError(), ConnectionResetError()])
def test_health_response_ignores_expected_client_disconnects(error: OSError) -> None:
    class DisconnectingWriter:
        def write(self, _body: bytes) -> None:
            raise error

    handler = SimpleNamespace(wfile=DisconnectingWriter())
    Handler._write(handler, b"health")
