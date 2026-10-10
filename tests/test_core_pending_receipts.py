"""Bounded receipt-only recovery, without rerunning desktop or execution calls."""
import pytest

from tests import test_core_integration as core


def test_pending_recovery_preserves_operation_token_and_images(monkeypatch):
    observed = []
    states = iter(['queued', 'running', 'succeeded'])
    image = {'content': [{'type': 'image', 'data': 'fixture-image'}]}
    def call(stack, name, args, token):
        observed.append((name, args, token))
        state = next(states)
        return {'operations': [{'operation_id': 'original', 'pending': state != 'succeeded', 'state': state,
            'result': {'data': {'session_id': 'fixture'}}}]}, image
    monkeypatch.setattr(core, 'call', call)
    data, response = core.finish(object(), {'operation_id': 'original', 'pending': True}, 'same-token')
    assert data == {'session_id': 'fixture'} and response is image
    assert len(observed) == 3
    assert all(name == 'process' and args == {'operation': 'wait', 'operation_ids': ['original'], 'wait_seconds': 5}
               and token == 'same-token' for name, args, token in observed)


def test_pending_recovery_has_a_deadline_without_restarting(monkeypatch):
    calls, times = [], iter([0, 0, 31])
    monkeypatch.setattr(core, 'monotonic', lambda: next(times))
    def call(stack, name, args, token):
        calls.append((name, args))
        return {'operations': [{'operation_id': 'original', 'pending': True, 'state': 'queued'}]}, {}
    monkeypatch.setattr(core, 'call', call)
    with pytest.raises(AssertionError, match='Original operation still pending'):
        core.finish(object(), {'operation_id': 'original', 'pending': True})
    assert len(calls) == 1 and calls[0][0] == 'process'


def test_failed_original_operation_is_never_reported_as_success(monkeypatch):
    monkeypatch.setattr(core, 'call', lambda *args: ({'operations': [
        {'operation_id': 'original', 'pending': False, 'state': 'failed', 'error': 'Fixture failure'}]}, {}))
    with pytest.raises(AssertionError, match='Fixture failure'):
        core.finish(object(), {'operation_id': 'original', 'pending': True})
