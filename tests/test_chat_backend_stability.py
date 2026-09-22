"""Deterministic native shutdown and delayed notification regressions."""
import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from agent import chat_worker
from agent.chat_worker import Protocol
from shared.native_cli import database


def codex_protocol():
    sent, events, finished = [], [], []
    protocol = Protocol('codex', {'cwd': '.'}, sent.append,
                        lambda kind, **values: events.append((kind, values)),
                        lambda *values: finished.append(values), lambda _: None)
    protocol.ready = True
    protocol.thread = 'thread'
    return protocol, sent, events, finished


@pytest.mark.parametrize('awaiting_start', [False, True])
def test_old_turn_notifications_cannot_settle_or_change_next_prompt(awaiting_start):
    protocol, sent, events, finished = codex_protocol()
    protocol.prompt('first', {'text': 'first'})
    protocol.receive({'id': sent[-1]['id'], 'result': {'turn': {'id': 'old'}}})
    protocol.receive({'method': 'turn/completed', 'params': {'turn': {'id': 'old', 'status': 'completed'}}})
    protocol.prompt('second', {'text': 'second'})
    if not awaiting_start:
        protocol.receive({'id': sent[-1]['id'], 'result': {'turn': {'id': 'new'}}})
    observed = len(events)
    for event in [
        {'method': 'turn/started', 'params': {'turn': {'id': 'old'}}},
        {'method': 'item/agentMessage/delta', 'params': {'turnId': 'old', 'delta': 'stale text'}},
        {'id': 'old-approval', 'method': 'item/commandExecution/requestApproval', 'params': {'turnId': 'old'}},
        {'method': 'turn/completed', 'params': {'turn': {'id': 'old', 'status': 'completed'}}},
    ]:
        protocol.receive(event)
    assert protocol.active == 'second'
    assert protocol.turn == (None if awaiting_start else 'new')
    assert finished == [('first', 'completed')]
    assert len(events) == observed
    assert not protocol.pending


@pytest.mark.parametrize('failed', [False, True])
def test_late_start_reply_cannot_overwrite_or_fail_next_turn(failed):
    protocol, sent, events, finished = codex_protocol()
    protocol.prompt('first', {'text': 'first'})
    old_call = sent[-1]['id']
    protocol.receive({'method': 'turn/started', 'params': {'turn': {'id': 'old'}}})
    protocol.receive({'method': 'turn/completed', 'params': {'turn': {'id': 'old', 'status': 'completed'}}})
    protocol.prompt('second', {'text': 'second'})
    protocol.receive({'id': sent[-1]['id'], 'result': {'turn': {'id': 'new'}}})
    protocol.receive({'id': old_call, **({'error': {'message': 'old error'}} if failed else {'result': {'turn': {'id': 'old'}}})})
    assert protocol.active == 'second'
    assert protocol.turn == 'new'
    assert finished == [('first', 'completed')]


def compact_event(method, turn, **extra):
    params = {'threadId': 'thread', 'turnId': turn, **extra}
    if method.startswith('turn/'):
        params['turn'] = {'id': turn, 'status': 'completed', **extra}
    else:
        params['item'] = {'id': 'item-' + turn, 'type': 'contextCompaction'}
    return {'method': method, 'params': params}


def test_consecutive_manual_compactions_close_idle_turns():
    protocol, sent, events, finished = codex_protocol()
    for turn in ('ct1', 'ct2'):
        protocol.command(turn, {'name': 'compact'})
        protocol.receive({'id': sent[-1]['id'], 'result': {}})
        protocol.receive(compact_event('turn/started', turn))
        protocol.receive(compact_event('item/completed', turn))
        protocol.receive(compact_event('turn/completed', turn))
        assert protocol.turn is None
        assert protocol.compaction_receipt is None
    assert finished == [('ct1', 'completed'), ('ct2', 'completed')]
    protocol.prompt('next', {'text': 'after compact'})
    protocol.receive({'id': sent[-1]['id'], 'result': {'turn': {'id': 'next-turn'}}})
    protocol.receive(compact_event('turn/completed', 'next-turn'))
    assert finished[-1] == ('next', 'completed')


@pytest.mark.parametrize('followup', ['compact', 'prompt'])
def test_completed_compaction_tail_cannot_settle_followup(followup):
    protocol, sent, events, finished = codex_protocol()
    protocol.command('first', {'name': 'compact'})
    old_call = sent[-1]['id']
    protocol.receive(compact_event('turn/started', 'old-compact'))
    protocol.receive(compact_event('item/completed', 'old-compact'))
    if followup == 'compact':
        protocol.command('second', {'name': 'compact'})
    else:
        protocol.prompt('second', {'text': 'next prompt'})
    protocol.receive(compact_event('item/completed', 'old-compact'))
    protocol.receive(compact_event('turn/completed', 'old-compact'))
    protocol.receive({'id': old_call, 'error': {'message': 'late old rejection'}})
    assert finished == [('first', 'completed')]
    assert protocol.compaction_receipt == ('second' if followup == 'compact' else None)
    assert protocol.active == ('second' if followup == 'prompt' else None)
    assert protocol.turn is None


@pytest.mark.parametrize('status, expected', [('completed', 'completed'), ('failed', 'error'), ('interrupted', 'interrupted')])
def test_idle_compaction_terminal_turn_always_releases_pending_command(status, expected):
    protocol, sent, events, finished = codex_protocol()
    protocol.command('compact', {'name': 'compact'})
    protocol.receive({'id': sent[-1]['id'], 'result': {}})
    protocol.receive(compact_event('turn/started', 'compact-turn'))
    protocol.receive(compact_event('turn/completed', 'compact-turn', status=status))
    assert protocol.compaction_receipt is None
    assert protocol.turn is None
    assert finished == [('compact', expected)]


@pytest.mark.skipif(os.name == 'nt', reason='Structured native chat requires POSIX pipes')
def test_worker_drains_completed_output_after_native_process_exits(tmp_path, monkeypatch):
    spool = tmp_path / 'spool'
    script = tmp_path / 'native.py'
    script.write_text('''import json, os, pathlib, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('type')
    if method == 'get_state':
        emit({'type': 'response', 'id': message['id'], 'success': True, 'data': {}})
    elif method == 'prompt':
        emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': 'prefix'}})
        deadline = time.monotonic() + 5
        while not pathlib.Path('finish-native').exists():
            if time.monotonic() > deadline:
                sys.exit(9)
            time.sleep(.005)
        emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': ' final answer'}})
        emit({'type': 'agent_settled'})
        os._exit(0)
''')
    sid, receipt = uuid.uuid4().hex, uuid.uuid4().hex
    with database(spool) as db:
        db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,argv,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (sid, 'project', 'device', str(tmp_path), str(tmp_path), 'pi', 'chat', 'starting', time.time(), time.time(), json.dumps([sys.executable, str(script)]), 'chat'))
        db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)',
                   (receipt, sid, 'chat_prompt', '{"text":"fixture"}', time.time()))
    children = []
    popen = subprocess.Popen
    receive = Protocol.receive

    def capture_child(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def receive_and_exit(protocol, message):
        receive(protocol, message)
        if message.get('assistantMessageEvent', {}).get('delta') == 'prefix':
            (tmp_path / 'finish-native').touch()
            # Force the process to exit between reads, leaving its final two
            # events in stdout. No timing-dependent sleeps in the assertion.
            # Observe exit without reaping: the worker must retain ownership of
            # the leader's PID until it has drained stdout and cleaned the group.
            info = os.waitid(os.P_PID, children[0].pid, os.WEXITED | os.WNOWAIT)
            assert info.si_status == 0

    monkeypatch.setattr(chat_worker.subprocess, 'Popen', capture_child)
    monkeypatch.setattr(chat_worker.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(Protocol, 'receive', receive_and_exit)
    chat_worker.run(spool, sid)
    with database(spool) as db:
        events = [json.loads(line) for line in b''.join(row[0] for row in db.execute('SELECT data FROM output ORDER BY offset')).splitlines()]
        assert db.execute('SELECT state FROM commands WHERE id=?', (receipt,)).fetchone()[0] == 'completed'
        assert ''.join(event['text'] for event in events if event['type'] == 'delta') == 'prefix final answer'
        assert [event['status'] for event in events if event['type'] == 'done'] == ['completed']
        assert db.execute('SELECT status FROM sessions').fetchone()[0] == 'exited'
