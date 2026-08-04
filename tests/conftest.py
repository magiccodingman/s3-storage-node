from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def wait_for_transport_chaos_public_gate(request, monkeypatch):
    """Allow HAProxy to observe each direct guardian-ready transition.

    The chaos harness polls the guardian health port directly, while public S3
    traffic crosses HAProxy. HAProxy intentionally observes that endpoint on a
    250 ms cadence, so a direct-ready snapshot can precede the public gate by
    one polling interval. This fixture applies only to the full transport chaos
    test and waits two intervals after every transport-ready transition.
    """

    path = getattr(request.node, "path", None)
    if path is None or path.name != "test_transport_chaos_integration.py":
        yield
        return

    module = request.module
    original_wait_ready = module._wait_ready

    def wait_ready(*args, **kwargs):
        snapshot = original_wait_ready(*args, **kwargs)
        time.sleep(0.6)
        return snapshot

    monkeypatch.setattr(module, "_wait_ready", wait_ready)
    yield
