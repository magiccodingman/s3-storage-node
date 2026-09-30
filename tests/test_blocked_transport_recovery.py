from unittest.mock import Mock

import pytest

from s3_storage_node.transport_failover import TransportSelector, TransportFailoverError, load_exclusive_failover
from s3_storage_node.transport_guardian import Guardian
from tests.test_transport_failover import make_config, write_config
from tests.test_transport_guardian import make_config as guardian_config, write_failover, fake_generation


def selector(tmp_path):
    path = tmp_path / 'config.toml'
    write_config(path)
    config = load_exclusive_failover(path, make_config(tmp_path))
    return TransportSelector(tmp_path / 'state/guardian', config)


def test_quarantine_survives_restart_and_cooldown_and_needs_clearance(tmp_path):
    first = selector(tmp_path)
    first.record_failure('cifs-primary', 'blocked', now=1)
    first.quarantine('cifs-primary', 'blocked task after fence')
    second = selector(tmp_path)
    assert second.select(now=1000000) == 'sshfs-secondary'
    with pytest.raises(TransportFailoverError, match='quarantined'):
        second.request('cifs-primary')
    second.record_success('cifs-primary')
    assert 'cifs-primary' in second.status()['quarantined']
    second.clear_quarantine('cifs-primary')
    assert second.status()['requested'] == ''
    second.request('cifs-primary')
    assert second.select() == 'cifs-primary'


def test_all_quarantined_refuses_selection_and_corrupt_journal_fails_closed(tmp_path):
    value = selector(tmp_path)
    for name in ('cifs-primary', 'sshfs-secondary'):
        value.quarantine(name, 'blocked')
    with pytest.raises(TransportFailoverError, match='all configured transports are quarantined'):
        value.select()
    value.path.write_text('corrupt journal')
    with pytest.raises(TransportFailoverError, match='cannot safely read'):
        value.select()


def test_existing_pending_request_cannot_bypass_quarantine(tmp_path):
    value = selector(tmp_path)
    value.request('cifs-primary')
    value.quarantine('cifs-primary', 'blocked')
    with pytest.raises(TransportFailoverError, match='quarantined'):
        value.select()


def test_blocked_tasks_park_and_resume_without_replacement_or_storage_calls(tmp_path):
    path = tmp_path / 'config.toml'
    write_failover(path)
    guardian = Guardian(guardian_config(tmp_path), str(path))
    guardian.active_transport = 'cifs-primary'
    child = Mock()
    child.process.pid = 1234
    child.running.side_effect = [True, True, False]
    guardian.lingering_processes = [child]
    guardian._begin_generation = Mock()
    guardian._run_helper = Mock()
    sleeps = []
    def sleep(seconds):
        snapshot = guardian.health.snapshot()
        assert snapshot['state'] == 'HOST_RECOVERY_REQUIRED'
        assert snapshot['ready'] is False
        assert snapshot['blocked_recovery']['pids'] == [1234]
        sleeps.append(seconds)
    guardian._interruptible_sleep = sleep
    guardian._wait_for_blocked_cleanup()
    assert sleeps == [5, 5]
    assert guardian.health.snapshot()['blocked_recovery'] == {}
    guardian._begin_generation.assert_not_called()
    guardian._run_helper.assert_not_called()


def test_surviving_writer_after_verified_fence_quarantines_transport(tmp_path, monkeypatch):
    from s3_storage_node.generation_guardian import Guardian as Base
    path = tmp_path / 'config.toml'
    write_failover(path)
    guardian = Guardian(guardian_config(tmp_path), str(path))
    guardian.active_transport = 'cifs-primary'
    child = Mock()
    child.running.return_value = True
    guardian.lingering_processes = [child]
    monkeypatch.setattr(Base, '_terminate_generation', lambda *args, **kwargs: True)
    assert guardian._terminate_generation('stalled', cause='storage_failure', phase='ONLINE')
    assert 'cifs-primary' in guardian.transport_selector.status()['quarantined']


def test_startup_certification_does_not_remount_quarantined_transport(tmp_path):
    path = tmp_path / 'config.toml'
    write_failover(path)
    guardian = Guardian(guardian_config(tmp_path), str(path))
    guardian.transport_selector.quarantine('cifs-primary', 'blocked')
    guardian.generation_factory.create = Mock(return_value=fake_generation(1))
    guardian._begin_generation()
    verified = []
    guardian._verify_transport_on_startup = verified.append
    guardian._verify_all_transports_on_startup()
    assert verified == ['sshfs-secondary']
    assert guardian.active_transport == 'sshfs-secondary'
    assert guardian.transport_selector.status()['startup_verified_transports'] == ['sshfs-secondary']


def test_pre_fence_diagnostics_precede_network_cut(tmp_path, monkeypatch):
    from tests.test_generation_fencing import make_guardian
    guardian = make_guardian(tmp_path)
    guardian.generation = fake_generation(4)
    guardian.generation_history.start(4, transport='cifs', mode='namespace')
    calls = []
    monkeypatch.setattr('s3_storage_node.generation_guardian.capture', lambda directory, generation, phase: calls.append(phase))
    guardian._stop_seaweed = Mock(side_effect=lambda **kwargs: calls.append('drain') or False)
    guardian._fence_generation = Mock(side_effect=lambda reason: calls.append('fence') or True)
    guardian._repair_targets = Mock(return_value=False)
    guardian._terminate_generation('stalled', cause='storage_failure', phase='ONLINE')
    assert calls == ['before-drain', 'drain', 'before-fence', 'fence']
