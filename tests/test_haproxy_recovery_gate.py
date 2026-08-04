from __future__ import annotations

from types import SimpleNamespace

from s3_storage_node.render import render_haproxy


def test_haproxy_uses_guardian_certification_as_single_fast_recovery_gate(tmp_path) -> None:
    config = SimpleNamespace(
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
        ),
        seaweed=SimpleNamespace(s3_internal_port=18333),
        worker_endpoint_host="169.254.254.2",
    )

    rendered = render_haproxy(config).read_text(encoding="utf-8")

    assert "addr 127.0.0.1 port 9090" in rendered
    assert "inter 250ms fall 1 rise 1" in rendered
    assert "rise 2" not in rendered
