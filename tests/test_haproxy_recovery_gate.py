from __future__ import annotations

from types import SimpleNamespace

from s3_storage_node.render import render_haproxy


def _config(tmp_path, *, admission_enabled: bool = True):
    return SimpleNamespace(
        appliance=SimpleNamespace(
            runtime_dir=tmp_path,
            health_host="0.0.0.0",
            health_port=9090,
        ),
        s3=SimpleNamespace(
            host="0.0.0.0",
            port=8333,
            tls_mode="off",
            tls_pem_file="",
            admission=SimpleNamespace(
                enabled=admission_enabled,
                max_active_read_requests=16,
                max_active_write_requests=2,
                max_queued_read_requests=32,
                max_queued_write_requests=16,
                queue_timeout_seconds=10,
            ),
        ),
        seaweed=SimpleNamespace(s3_internal_port=18333),
        worker_endpoint_host="169.254.254.2",
    )


def test_haproxy_uses_guardian_certification_as_single_fast_recovery_gate(tmp_path) -> None:
    rendered = render_haproxy(_config(tmp_path)).read_text(encoding="utf-8")

    assert "addr 127.0.0.1 port 9090" in rendered
    assert "inter 250ms fall 1 rise 1" in rendered
    assert "rise 2" not in rendered


def test_haproxy_bounds_active_and_queued_s3_requests(tmp_path) -> None:
    rendered = render_haproxy(_config(tmp_path)).read_text(encoding="utf-8")

    assert "acl s3_read method GET HEAD OPTIONS" in rendered
    assert "srv_queue(seaweed_s3_read/worker_s3_read) ge 32" in rendered
    assert "srv_queue(seaweed_s3_write/worker_s3_write) ge 16" in rendered
    assert "use_backend seaweed_s3_read if s3_read" in rendered
    assert "option abortonclose" in rendered
    assert "timeout queue 10s" in rendered
    assert "maxconn 16 maxqueue 32" in rendered
    assert "maxconn 2 maxqueue 16" in rendered


def test_haproxy_can_disable_s3_admission_limits(tmp_path) -> None:
    rendered = render_haproxy(
        _config(tmp_path, admission_enabled=False)
    ).read_text(encoding="utf-8")

    assert "s3_read_queue_full" not in rendered
    assert "timeout queue" not in rendered
    assert "option abortonclose" not in rendered
    assert "maxqueue" not in rendered
    assert "maxconn 16" not in rendered
