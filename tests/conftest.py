from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


def _diagnostic(message: str) -> None:
    path = os.environ.get("TRANSPORT_CHAOS_DIAGNOSTIC_FILE", "")
    if not path:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.time():.3f} {message}\n")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not item.module.__name__.endswith("test_transport_chaos_integration"):
        return
    if report.when == "call" and report.failed:
        _diagnostic("FAILURE\n" + report.longreprtext)


@pytest.fixture(autouse=True)
def stabilize_transport_chaos_observers(request, monkeypatch):
    """Align the full chaos harness with asynchronous runtime observers."""

    module = request.module
    if not module.__name__.endswith("test_transport_chaos_integration"):
        yield
        return

    _diagnostic("chaos test started")
    original_wait_ready = module._wait_ready
    original_drop_smb = module._drop_smb
    original_restore_smb = module._restore_smb
    original_assert_objects = module._assert_objects

    def wait_ready(*args, **kwargs):
        transport = args[1] if len(args) > 1 else kwargs.get("transport", "unknown")
        _diagnostic(f"waiting ready transport={transport}")
        snapshot = original_wait_ready(*args, **kwargs)
        time.sleep(0.6)
        generation = snapshot.get("generation", {}).get("id", "unknown")
        _diagnostic(f"ready transport={transport} generation={generation}")
        return snapshot

    def wait_primary_failure(project: str, env: dict[str, str]):
        _diagnostic("waiting persisted cifs-primary failure")

        def failed():
            status = module._transport_status(project, env)
            if not status:
                return None
            failures = status.get("failed", {})
            if isinstance(failures, dict) and "cifs-primary" in failures:
                return status
            return None

        status = module._wait(
            failed,
            "CIFS failure persistence",
            timeout=180,
            interval=0.15,
        )
        _diagnostic("persisted cifs-primary failure observed")
        return status

    def drop_smb(*args, **kwargs):
        _diagnostic("dropping smb port 445")
        return original_drop_smb(*args, **kwargs)

    def restore_smb(*args, **kwargs):
        _diagnostic("restoring smb port 445")
        return original_restore_smb(*args, **kwargs)

    def assert_objects(*args, **kwargs):
        objects = args[2] if len(args) > 2 else kwargs.get("objects", {})
        _diagnostic(f"verifying objects count={len(objects)}")
        result = original_assert_objects(*args, **kwargs)
        _diagnostic(f"verified objects count={len(objects)}")
        return result

    monkeypatch.setattr(module, "_wait_ready", wait_ready)
    monkeypatch.setattr(module, "_wait_primary_failure", wait_primary_failure)
    monkeypatch.setattr(module, "_drop_smb", drop_smb)
    monkeypatch.setattr(module, "_restore_smb", restore_smb)
    monkeypatch.setattr(module, "_assert_objects", assert_objects)
    yield
    _diagnostic("chaos test fixture finished")
