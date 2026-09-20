"""Fault-injection coverage for the complete stop/swap recovery boundary."""
import json
import pytest
from tests.test_agent_lifecycle import helper, write_runtime
from shared.util import atomic_json


def setup_runtime(tmp_path):
    base = tmp_path / 'agent'
    candidate = base / '.runtime-update-fault'
    write_runtime(base / 'runtime', 'old')
    write_runtime(candidate, 'new')
    atomic_json(base / 'management.json', {'service_kind': 'systemd', 'service_scope': 'user',
                                         'installed_version': 'old', 'status': 'ready'})
    return base, candidate


@pytest.mark.parametrize('point', ['backup_cleanup', 'failed_cleanup', 'first_rename'])
def test_preswap_failure_restarts_original_service(tmp_path, monkeypatch, point):
    base, candidate = setup_runtime(tmp_path)
    calls = []
    monkeypatch.setattr(helper, 'stop_service', lambda *a, **k: calls.append('stop'))
    monkeypatch.setattr(helper, '_wait_for_exit', lambda *a: None)
    monkeypatch.setattr(helper, 'start_service', lambda *a: calls.append('start'))
    monkeypatch.setattr(helper, 'verify_service', lambda *a: calls.append('verify'))
    remove, move = helper._remove, helper._move
    def injected_remove(path):
        if ((point == 'backup_cleanup' and path.name == '.runtime-previous') or
                (point == 'failed_cleanup' and path.name == '.runtime-failed')):
            raise OSError('injected pre-swap fault')
        return remove(path)
    def injected_move(source, destination):
        if point == 'first_rename' and source == base / 'runtime':
            raise OSError('injected pre-swap fault')
        return move(source, destination)
    monkeypatch.setattr(helper, '_remove', injected_remove)
    monkeypatch.setattr(helper, '_move', injected_move)
    with pytest.raises(OSError, match='injected pre-swap fault'):
        helper.apply_update(base, candidate, 123)
    assert calls == ['stop', 'start', 'verify']
    assert helper._version(base / 'runtime') == 'old'
    assert candidate.is_dir()
    assert json.loads((base / 'management.json').read_text())['status'] == 'rollback'


def test_failed_recovery_does_not_claim_verified_rollback(tmp_path, monkeypatch):
    base, candidate = setup_runtime(tmp_path)
    monkeypatch.setattr(helper, 'stop_service', lambda *a, **k: None)
    monkeypatch.setattr(helper, '_wait_for_exit', lambda *a: None)
    def fail_remove(*a): raise OSError('cleanup denied')
    def fail_start(*a): raise OSError('original service start failed')
    monkeypatch.setattr(helper, '_remove', fail_remove)
    monkeypatch.setattr(helper, 'start_service', fail_start)
    with pytest.raises(RuntimeError, match='自动恢复未完成'):
        helper.apply_update(base, candidate, 123)
    data = json.loads((base / 'management.json').read_text())
    assert data['status'] == 'error'
    assert '已恢复' not in data['last_error']
    assert helper._version(base / 'runtime') == 'old'


def test_stop_failure_verifies_existing_service_without_duplicate_start(tmp_path, monkeypatch):
    base, candidate = setup_runtime(tmp_path)
    calls = []
    def fail_stop(*a): raise TimeoutError('stop command timed out')
    monkeypatch.setattr(helper, 'stop_service', fail_stop)
    monkeypatch.setattr(helper, 'verify_service', lambda *a: calls.append('verify'))
    monkeypatch.setattr(helper, 'start_service', lambda *a: calls.append('start'))
    with pytest.raises(TimeoutError): helper.apply_update(base, candidate, 123)
    assert calls == ['verify']
    assert helper._version(base / 'runtime') == 'old'
