from pathlib import Path
from types import SimpleNamespace

import pytest

from s3_storage_node.admission import AdmissionController, WriteBudget, write_stats
from s3_storage_node.config_types import S3AdmissionConfig
from s3_storage_node.health import HealthState
from s3_storage_node.render import render_haproxy


def test_budget_starts_low_and_increases_only_after_full_healthy_window():
    budget = WriteBudget(3, 300)
    assert budget.update(0, True) == 1
    assert budget.update(299, True) == 1
    assert budget.update(300, True) == 2
    assert budget.update(599, True) == 2
    assert budget.update(600, True) == 3
    assert budget.update(900, True) == 3
    assert budget.update(901, False) == 1
    assert budget.backoffs == 1
    assert budget.update(902, True) == 1
    assert budget.update(1201, True) == 1
    assert budget.update(1202, True) == 2


def test_priority_and_runtime_socket_rendering(tmp_path):
    config = SimpleNamespace(
        appliance=SimpleNamespace(runtime_dir=tmp_path, health_host="0.0.0.0", health_port=9090),
        s3=SimpleNamespace(host="0.0.0.0", port=8333, tls_mode="off", admission=S3AdmissionConfig()),
        seaweed=SimpleNamespace(s3_internal_port=18333), worker_endpoint_host="169.254.254.2",
    )
    content = render_haproxy(config).read_text()
    assert f"stats socket {tmp_path}/admission/control.sock mode 600 level admin" in content
    assert "maxconn 1 maxqueue 4" in content
    assert "set-priority-class int(10) if !s3_read s3_bulk_path" in content
    assert "set-priority-class int(10) if !s3_read s3_bulk_query" in content
    assert "hdr Retry-After 3" in content
    assert "<Code>SlowDown</Code>" in content
    assert "timeout queue 3s" in content


def test_write_stats_requires_exact_server():
    assert write_stats("# pxname,svname,qcur,scur\nseaweed_s3_write,BACKEND,9,9\nseaweed_s3_write,worker_s3_write,2,1\n") == (9, 1)
    with pytest.raises(ValueError):
        write_stats("# pxname,svname,qcur,scur\n")


@pytest.mark.parametrize("pressure", ["slow", "inflight", "queue", "offline", "stale", "error"])
def test_controller_backs_off_before_probe_timeout(monkeypatch, pressure):
    import s3_storage_node.admission as module
    health = HealthState()
    health.set("ONLINE", True)
    health.record_probe(True, 0.1)
    config = SimpleNamespace(
        appliance=SimpleNamespace(runtime_dir=Path("/tmp"), probe_interval_seconds=5, probe_timeout_seconds=15),
        s3=SimpleNamespace(admission=S3AdmissionConfig()),
    )
    controller = AdmissionController(config, health, lambda: False)
    controller.budget.limit = 2
    if pressure == "slow":
        health.record_probe(True, 2.1)
    elif pressure == "inflight":
        health.start_probe()
        monkeypatch.setattr(module.time, "monotonic", lambda: health.probe_started_monotonic + 3)
    elif pressure == "offline":
        health.set("RECOVERING", False)
    elif pressure == "stale":
        health.last_probe_at -= 100
    commands = []
    def runtime(path, command):
        commands.append(command)
        if command == "show stat":
            if pressure == "error":
                raise OSError("socket unavailable")
            queue = 1 if pressure == "queue" else 0
            return f"# pxname,svname,qcur,scur\nseaweed_s3_write,worker_s3_write,{queue},1\n"
        return "\n"
    monkeypatch.setattr(module, "runtime_command", runtime)
    controller.tick()
    assert controller.budget.limit == 1
    assert "set maxconn server seaweed_s3_write/worker_s3_write 1" in commands
    assert health.snapshot()["admission"]["applied"] == (pressure != "error")
