from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def stabilize_transport_chaos_observers(request, monkeypatch):
    """Align the full chaos harness with asynchronous runtime observers.

    The harness polls the guardian health port directly, while public S3 traffic
    crosses HAProxy. HAProxy observes readiness on a 250 ms cadence, so a direct
    ready snapshot can precede the public gate by one polling interval.

    The transport selector status uses the persisted `failed` map. Keeping this
    observer here prevents the long-running acceptance test from waiting on the
    obsolete `failures` spelling while the real failover has already succeeded.
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

    def wait_primary_failure(project: str, env: dict[str, str]):
        def failed():
            status = module._transport_status(project, env)
            if not status:
                return None
            failures = status.get("failed", {})
            if isinstance(failures, dict) and "cifs-primary" in failures:
                return status
            return None

        return module._wait(
            failed,
            "CIFS failure persistence",
            timeout=180,
            interval=0.15,
        )

    monkeypatch.setattr(module, "_wait_ready", wait_ready)
    monkeypatch.setattr(module, "_wait_primary_failure", wait_primary_failure)
    yield
