"""Native event semantics from flow audit F10/F11, without launching a model."""
from __future__ import annotations

import pytest

from agent.chat_worker import Protocol


def pi_protocol(tmp_path):
    events, finished = [], []
    row = {'id': 'a' * 32, 'project_id': 'fixture-project', 'provider': 'pi',
           'mode': 'chat', 'root': str(tmp_path), 'cwd': str(tmp_path),
           'native_thread': '', 'chat_settings': '{}', 'argv': '[]'}
    protocol = Protocol('pi', row, lambda *_args, **_kwargs: None,
                        lambda kind, **fields: events.append({'type': kind, **fields}),
                        lambda receipt, state: finished.append((receipt, state)),
                        lambda *_args, **_kwargs: None)
    protocol.active = 'fixture-receipt'
    return protocol, events, finished


@pytest.mark.parametrize('success', [True, False])
def test_pi_final_receipt_uses_retry_outcome(tmp_path, success):
    protocol, events, finished = pi_protocol(tmp_path)
    protocol.receive({'type': 'message_end', 'message': {
        'role': 'assistant', 'content': [], 'stopReason': 'error',
        'errorMessage': '429 fixture rate limit'}})
    protocol.receive({'type': 'auto_retry_end', 'success': success,
                      **({} if success else {'errorMessage': 'fixture retries exhausted'})})
    protocol.receive({'type': 'agent_settled'})
    expected = 'completed' if success else 'error'
    assert finished == [('fixture-receipt', expected)]
    done = [event for event in events if event['type'] == 'done']
    assert len(done) == 1 and done[0]['status'] == expected
    assert done[0]['text'] == ('' if success else 'fixture retries exhausted')


@pytest.mark.parametrize('is_error', [True, False])
def test_pi_tool_end_keeps_native_error_semantics(tmp_path, is_error):
    protocol, events, _finished = pi_protocol(tmp_path)
    protocol.receive({'type': 'tool_execution_end', 'toolCallId': 'fixture-tool',
                      'toolName': 'bash', 'isError': is_error,
                      'result': {'content': [{'type': 'text', 'text': 'fixture result'}]}})
    tool = next(event for event in events if event['type'] == 'tool')
    assert tool['tool_id'] == 'fixture-tool'
    assert tool['isError'] is is_error
    assert tool['status'] == ('error' if is_error else 'end')
