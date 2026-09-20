"""Desktop lease recovery and human-wait fences, without real desktop input."""
import asyncio
import copy
import json
import time

import pytest

from agent.computer import Computer, validate_computer
from shared.computer_contracts import NATIVE_ACTIONS
from shared.util import DevError
from tests.fake_computer_provider import frame
from tests.test_computer import CONFIG, PROJECT, FakeClient, opened


def test_approval_timeout_has_an_independent_bounded_local_setting():
    defaults = validate_computer({})
    assert defaults['call_timeout_seconds'] == 45
    assert defaults['approval_timeout_seconds'] == 60
    assert validate_computer({'call_timeout_seconds': 5, 'approval_timeout_seconds': 90})['approval_timeout_seconds'] == 90
    for value in (True, 4, 91, 60.5, '60'):
        with pytest.raises(ValueError):
            validate_computer({'approval_timeout_seconds': value})


@pytest.mark.asyncio
async def test_status_recovers_only_the_same_owner_project_and_root(tmp_path):
    manager, _, observed = await opened(tmp_path)
    try:
        own = await manager.execute('computer_status', PROJECT, {})
        assert own['active_session_id'] == observed['session_id']
        assert own['active_session']['app'] == 'Fixture'
        assert own['active_session']['expires_at'] == manager.session['expires_at']
        assert 0 < own['active_session']['remaining_seconds'] <= 300
        for different in ({'_computer_owner': 'grant:other'}, {'id': 'p2'}, {'root': '/other'}):
            status = await manager.execute('computer_status', {**PROJECT, **different}, {})
            assert status['session_active']
            assert status['active_session_id'] is None and status['active_session'] is None
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['observe', 'preflight'])
async def test_failed_read_releases_the_dead_lease_without_input(tmp_path, stage):
    manager, _, observed = await opened(tmp_path)
    client = manager.session['client']
    async def fail(name, args):
        client.closed = True
        raise DevError('COMPUTER_DISCONNECTED', 'private native detail')
    client.call = fail
    try:
        args = {'session_id': observed['session_id']}
        tool = 'computer_observe'
        if stage == 'preflight':
            tool = 'computer_action'
            args.update(observation_id=observed['observation_id'], action={'type': 'press_key', 'key': 'Return'})
        with pytest.raises(DevError) as error:
            await manager.execute(tool, PROJECT, args)
        assert error.value.code == 'COMPUTER_DISCONNECTED'
        assert client.closed and manager.session is None
        assert client.value == 0
        fresh = await manager.execute('computer_session_open', PROJECT, {'app': 'Fixture', 'ttl_seconds': 300})
        assert fresh['session_id'] != observed['session_id']
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_post_action_read_failure_preserves_receipt_and_releases_lease(tmp_path):
    manager, _, observed = await opened(tmp_path)
    client = manager.session['client']
    original = client.call
    async def fail_after_action(name, args):
        if name == 'get_app_state' and client.value:
            client.closed = True
            raise DevError('COMPUTER_TIMEOUT', 'private native detail')
        return await original(name, args)
    client.call = fail_after_action
    try:
        result = await manager.execute('computer_action', PROJECT, {
            'session_id': observed['session_id'], 'observation_id': observed['observation_id'],
            'action': {'type': 'type_text', 'text': 'once'},
        })
        assert result['action_outcome'] == 'completed'
        assert result['observation_error'] == 'COMPUTER_TIMEOUT'
        assert result['observation_id'] is None
        assert result['session_closed'] and result['next'] == 'computer_session_open'
        assert client.value == 1 and client.closed and manager.session is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_native_phase_separates_approval_and_native_elapsed_time(tmp_path):
    manager = Computer(lambda: copy.deepcopy(CONFIG), tmp_path, client_factory=FakeClient)
    client = FakeClient()
    client.approval_wait_ms = 700
    phases = []
    async def native_call(name, args):
        await asyncio.sleep(.04)
        client.approval_wait_ms += 20
        return frame()
    client.call = native_call
    await manager.native(client, 'get_app_state', {'app': 'private app'}, 'native_observe',
                         lambda stage, **metadata: phases.append(metadata))
    completed = phases[-1]
    assert completed['outcome'] == 'completed'
    assert completed['approval_wait_ms'] == 20
    assert completed['native_call_ms'] + completed['approval_wait_ms'] == completed['duration_ms']
    assert 'private' not in json.dumps(phases)


@pytest.mark.asyncio
async def test_invalid_configuration_cannot_kill_the_expiry_watchdog(tmp_path):
    manager, config, _ = await opened(tmp_path)
    client = manager.session['client']
    try:
        config['computer'] = {'enabled': 'invalid'}
        await asyncio.sleep(1.1)
        assert manager.session is None and client.closed
        assert manager.guard and not manager.guard.done()
        with pytest.raises(DevError) as error:
            await manager.execute('computer_status', PROJECT, {})
        assert error.value.code == 'COMPUTER_CONFIG_INVALID'
        config['computer'] = copy.deepcopy(CONFIG['computer'])
        assert (await manager.execute('computer_session_open', PROJECT, {'app': 'Fixture', 'ttl_seconds': 300}))['session_id']
    finally:
        await manager.close()


class Consent:
    def __init__(self, delay=0):
        self.delay = delay
        self.timeouts = []
        self.was_valid = []
    async def request(self, context, message, valid, timeout):
        self.timeouts.append(timeout)
        await asyncio.sleep(self.delay)
        self.was_valid.append(valid())
        # Deliberately return a late accept: the Computer boundary must fence it too.
        return {'action': 'accept', 'content': {}}
    def cancel(self, session_id):
        pass


@pytest.mark.asyncio
async def test_preflight_approval_expiry_never_extends_first_input_deadline(tmp_path):
    manager, _, observed = await opened(tmp_path)
    client = manager.session['client']
    manager.approvals = Consent(delay=.06)
    original = client.call
    decisions = []
    async def request_approval(name, args):
        if name == 'get_app_state':
            decisions.append(await client.approval_handler('private approval message'))
            return frame(client.value)
        return await original(name, args)
    client.call = request_approval
    try:
        with pytest.raises(DevError) as error:
            await manager.execute('computer_action', PROJECT, {
                'session_id': observed['session_id'], 'observation_id': observed['observation_id'],
                'action': {'type': 'press_key', 'key': 'Return'},
            }, not_after=time.time()+.03, operation_id='op')
        assert error.value.code == 'QUEUE_EXPIRED'
        assert decisions == [{'action': 'cancel'}]
        assert 0 < manager.approvals.timeouts[0] <= .03
        assert manager.approvals.was_valid == [False]
        assert not any(name in NATIVE_ACTIONS for name, _ in client.calls)
        assert manager.session['observation'] is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_post_action_observation_uses_independent_approval_budget(tmp_path):
    config = copy.deepcopy(CONFIG)
    config['computer'].update(call_timeout_seconds=5, approval_timeout_seconds=90)
    consent = Consent()
    manager = Computer(lambda: config, tmp_path, client_factory=FakeClient, approvals=consent)
    phases = []
    phase = lambda stage, **metadata: phases.append((stage, metadata))
    try:
        opened_result = await manager.execute('computer_session_open', PROJECT, {'app': 'Fixture', 'ttl_seconds': 300}, phase=phase)
        observed = await manager.execute('computer_observe', PROJECT, {'session_id': opened_result['session_id']}, phase=phase)
        client = manager.session['client']
        assert client.approval_timeout_seconds == 90
        original = client.call
        decisions = []
        async def slow_action_then_consent(name, args):
            if name in NATIVE_ACTIONS:
                result = await original(name, args)
                await asyncio.sleep(.06)
                return result
            if name == 'get_app_state' and client.value:
                decisions.append(await client.approval_handler('private approval message'))
            return await original(name, args)
        client.call = slow_action_then_consent
        result = await manager.execute('computer_action', PROJECT, {
            'session_id': observed['session_id'], 'observation_id': observed['observation_id'],
            'action': {'type': 'type_text', 'text': 'private typed content'},
        }, not_after=time.time()+.03, operation_id='op', phase=phase)
        assert result['action_outcome'] == 'completed' and result['observation_id']
        assert decisions == [{'action': 'accept', 'content': {}}]
        assert consent.timeouts == [90] and consent.was_valid == [True]
        names = {stage for stage, _ in phases}
        assert {'native_startup', 'native_observe', 'native_preflight', 'native_action', 'approval_wait', 'approval_decided'} <= names
        assert 'private' not in json.dumps(phases)
        assert all(set(metadata) <= {'outcome', 'duration_ms', 'approval_timeout_seconds', 'error_code', 'provider_exit_code', 'approval_wait_ms', 'native_call_ms'} for _, metadata in phases)
    finally:
        await manager.close()
