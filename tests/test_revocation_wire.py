"""Real loopback grant revocation preserves started work and cancels queued reads."""
import sys
import uuid

import pytest

from shared.util import atomic_json
from tests.support import running_stack, wait_for


@pytest.mark.integration
@pytest.mark.slow
def test_revocation_wire_fences_accepted_read_but_preserves_running_command(tmp_path):
    with running_stack(tmp_path / 'revocation-stack') as stack:
        devices = stack.client.get('/api/devices').json()['devices']
        device = next(item for item in devices if item['id'] == stack.device)
        info = device['info']
        if isinstance(info, str):
            import json
            info = json.loads(info)
        assert info['cancel_pending_protocol'] == 1
        release = stack.projectalpha / 'release-held-command'
        code = ("import pathlib,time; print('HOLDING',flush=True); "
                "p=pathlib.Path('release-held-command'); deadline=time.monotonic()+60; "
                "\nwhile not p.exists() and time.monotonic()<deadline: time.sleep(.05)"
                "\nassert p.exists(); print('RELEASED',flush=True)")
        stack.config['tasks']['revocation-hold'] = {
            'command': [sys.executable, '-u', '-c', code], 'projects': ['ProjectAlpha'], 'timeout': 65}
        atomic_json(stack.config_path, stack.config)
        wait_for(lambda: any(task['name'] == 'revocation-hold'
                             for task in stack.fs('tasks_list')['tasks']))
        held = stack.mcp('exec', {'project': 'ProjectAlpha', 'task': 'revocation-hold', 'yield_seconds': 0,
                         'idempotency_key': uuid.uuid4().hex})['structuredContent']
        hold_id = held['operation_id']
        try:
            wait_for(lambda: 'HOLDING' in stack.client.get('/api/operations/' + hold_id).json().get('output', ''))
            queued = stack.mcp('read', {'project': 'ProjectAlpha', 'path': 'README.md'})['structuredContent']
            read_id = queued['operation_id']
            assert queued['pending']
            wait_for(lambda: stack.client.get('/api/operations/' + read_id).json().get('accepted_at'))
            stack.must(stack.client.delete('/api/grants/' + stack.grant))
            read_result = stack.poll(read_id, timeout=25)
            assert read_result['state'] == 'cancelled'
            assert read_result['result']['error']['code'] == 'CANCELLED'
            current = stack.client.get('/api/operations/' + hold_id).json()
            assert current['pending'] and not current['cancel_requested']
        finally:
            release.write_text('release only the owned fixture process')
        completed = stack.poll(hold_id, timeout=10)
        assert completed['state'] == 'succeeded'
        assert completed['result']['data']['exit_code'] == 0
        assert 'RELEASED' in completed['output']
