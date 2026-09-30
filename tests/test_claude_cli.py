"""Claude catalog, real worker pipes, admission and guarded permissions; no inference."""
from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from agent.chat_catalog import probe, validate_settings
from agent.claude_cli import launch
from agent.claude_protocol import ClaudeProtocol
from shared.native_cli import database
from shared.util import DevError
from tests.claude_fixture import FAKE_CLAUDE
from tests.support import wait_for
from tests.test_native_cli import native  # noqa: F401
from tests.test_chat_admission import chat  # noqa: F401
from tests.test_chat_sse import service  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def executable(path):
    path.write_text('#!' + sys.executable + '\n' + FAKE_CLAUDE)
    path.chmod(0o700)
    return str(path)


@pytest.fixture
def worker(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    home = tmp_path / 'home'; home.mkdir()
    program = executable(tmp_path / 'claude')
    directory = tmp_path / 'spool'
    db = database(directory)
    sid = uuid.uuid4().hex
    db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,mode,argv,chat_settings) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (sid, 'project', 'device', str(root), str(root), 'claude', 'Claude test', 'starting', time.time(), time.time(), 'chat', json.dumps(launch(program)), json.dumps({'next': {}})))
    db.commit()
    env = {'HOME': str(home), 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.defpath}
    process = subprocess.Popen([sys.executable, '-m', 'agent.chat_worker', str(directory), sid], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def events():
        raw = b''.join(r[0] for r in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset', (sid,)))
        return [json.loads(line) for line in raw.splitlines()]
    def command(kind, payload=None):
        receipt = uuid.uuid4().hex
        db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)', (receipt, sid, kind, json.dumps(payload or {}), time.time()))
        db.commit()
        return receipt
    def state(receipt):
        row = db.execute('SELECT state FROM commands WHERE id=?', (receipt,)).fetchone()
        return row[0] if row else None
    try:
        wait_for(lambda: any(e['type'] == 'settings' for e in events()), 10)
        yield SimpleNamespace(root=root, db=db, sid=sid, events=events, command=command, state=state, process=process)
    finally:
        if process.poll() is None:
            command('stop')
        try:
            out, err = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); out, err = process.communicate(timeout=5)
        db.close()
        assert process.returncode == 0, (out, err)


def protocol():
    sent, events, finished = [], [], []
    p = None
    def emit(kind, **values):
        events.append({'type': kind, 'receipt': p.active, **values})
    p = ClaudeProtocol('claude', {'cwd': '/fixture', 'chat_settings': '{}'}, sent.append, emit,
                       lambda receipt, state: finished.append((receipt, state)), lambda thread: None)
    p.start()
    p.receive({'type': 'control_response', 'response': {'request_id': sent[-1]['request_id'], 'subtype': 'success', 'response': {
        'models': [{'value': 'sonnet', 'displayName': 'Sonnet', 'supportedEffortLevels': ['low', 'high']}], 'commands': []}}})
    return p, sent, events, finished


def permission(p, key='request', tool='Bash', inputs=None):
    p.receive({'type': 'control_request', 'request_id': key,
               'request': {'subtype': 'can_use_tool', 'tool_name': tool, 'input': inputs if inputs is not None else {'command': 'echo fixture'}}})


def test_catalog_inference_free_and_secret_allowlist(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    program = executable(tmp_path / 'claude')
    data = probe('claude', program, tmp_path, {'HOME': str(home), 'PATH': os.defpath}, 'sonnet')
    assert data['model']['id'] == 'sonnet'
    assert data['thinking_levels'] == ['low', 'high', 'max']
    assert data['capabilities']['steer'] is False
    assert data['capabilities']['effort_runtime'] is False
    assert {c['name'] for c in data['commands']} == {'compact', 'fixture-skill'}
    assert 'must-not-leak' not in json.dumps(data)
    wire = [json.loads(x) for x in (tmp_path / 'claude-wire.jsonl').read_text().splitlines()]
    assert len(wire) == 1 and wire[0]['request']['subtype'] == 'initialize'
    assert '--no-session-persistence' in json.loads((tmp_path / 'claude-argv.json').read_text())


def test_launch_native_defaults_literals_and_resume():
    sid = str(uuid.uuid4())
    argv = launch('/fixture/claude', {'model': 'custom;not-a-shell', 'effort': 'max'}, sid)
    assert argv[argv.index('--model') + 1] == 'custom;not-a-shell'
    assert argv[-2:] == ['--resume', sid]
    assert '--permission-prompt-tool' in argv and 'stdio' in argv
    assert not any('bypass' in x or 'skip-permission' in x for x in argv)
    with pytest.raises(ValueError): launch('claude', resume='../other-history')
    with pytest.raises(ValueError): validate_settings({'effort': 'off'}, 'claude')
    with pytest.raises(ValueError): validate_settings({'model': 'a\n--unsafe'}, 'claude')


def test_real_worker_stream_final_dedup_usage_and_continuation(worker):
    w = worker; receipt = w.command('chat_prompt', {'text': 'first'})
    wait_for(lambda: w.state(receipt) == 'completed', 8)
    events = [e for e in w.events() if e.get('receipt') == receipt]
    deltas = [e for e in events if e['type'] == 'delta']
    messages = [e for e in events if e['type'] == 'message']
    assert ''.join(e['text'] for e in deltas) == 'hello 世界'
    assert len(messages) == 1 and messages[0]['item_id'] == deltas[0]['item_id']
    reasoning = [e for e in events if e['type'] == 'reasoning']
    assert ''.join(e['text'] for e in reasoning) == 'fixture thought'
    assert reasoning[-1]['status'] == 'end'
    assert len([e for e in events if e['type'] == 'user']) == 1
    assert any(e['type'] == 'tool' and e['status'] == 'end' for e in events)
    stats = next(e['stats'] for e in events if e['type'] == 'stats')
    assert stats['tokens']['total'] == 25 and stats['cost'] == 0.001
    native_id = w.db.execute('SELECT native_thread FROM sessions WHERE id=?', (w.sid,)).fetchone()[0]
    assert str(uuid.UUID(native_id)) == native_id
    second = w.command('chat_prompt', {'text': 'second'})
    wait_for(lambda: w.state(second) == 'completed', 8)
    wire = [json.loads(x) for x in (w.root / 'claude-wire.jsonl').read_text().splitlines()]
    assert [m for m in wire if m['type'] == 'user'][-1]['session_id'] == native_id


def test_split_blocks_keep_stream_identity_and_distinct_repeated_text():
    p, sent, events, done = protocol();p.prompt('turn', {'text':'inspect'})
    def stream(value):p.receive({'type':'stream_event','event':value})
    stream({'type':'message_start','message':{'id':'main-message'}})
    stream({'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}})
    stream({'type':'content_block_stop','index':0})
    for index in (1, 2):
        stream({'type':'content_block_start','index':index,'content_block':{'type':'text','text':''}})
        stream({'type':'content_block_delta','index':index,'delta':{'type':'text_delta','text':'same'}})
        frame={'type':'assistant','uuid':f'block-{index}','message':{'id':'main-message','content':[{'type':'text','text':'same complete'}]}}
        p.receive(frame);p.receive(frame)
        stream({'type':'content_block_stop','index':index})
    deltas=[e for e in events if e['type']=='delta']
    finals=[e for e in events if e['type']=='message']
    assert [e['item_id'] for e in deltas]==['main-message:1','main-message:2']
    assert [e['item_id'] for e in finals]==['main-message:1','main-message:1','main-message:2','main-message:2']
    assert all(e['text']=='same complete' for e in finals)


def test_final_only_subagent_blocks_do_not_overwrite_each_other_or_main():
    p, sent, events, done = protocol();p.prompt('turn', {'text':'inspect'})
    for parent in ('tool-a','tool-b'):
        for index in (0,1):
            p.receive({'type':'assistant','uuid':f'final-{index}','parent_tool_use_id':parent,
                       'message':{'id':'shared-message','content':[{'type':'text','text':f'{parent} block {index}'}]}})
    finals=[e for e in events if e['type']=='message']
    assert len({e['item_id'] for e in finals})==4


def test_accumulated_snapshot_still_replaces_original_stream_blocks():
    p, sent, events, done = protocol();p.prompt('turn', {'text':'inspect'})
    for value in ({'type':'message_start','message':{'id':'snapshot'}},
                  {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}},
                  {'type':'content_block_start','index':1,'content_block':{'type':'text','text':''}},
                  {'type':'content_block_delta','index':1,'delta':{'type':'text_delta','text':'partial'}}):
        p.receive({'type':'stream_event','event':value})
    p.receive({'type':'assistant','message':{'id':'snapshot','content':[
        {'type':'thinking','thinking':'thought'},{'type':'text','text':'complete'}]}})
    assert next(e for e in events if e['type']=='message')['item_id']=='snapshot:1'


def test_worker_queue_interrupt_ack_and_error_result(worker):
    w = worker; hold = w.command('chat_prompt', {'text': 'hold'})
    wait_for(lambda: any(e['type'] == 'user' and e.get('receipt') == hold for e in w.events()))
    follow = w.command('chat_prompt', {'text': 'follow'})
    interrupt = w.command('chat_interrupt')
    wait_for(lambda: w.state(follow) == 'completed', 8)
    assert w.state(hold) == 'interrupted' and w.state(interrupt) == 'completed'
    failed = w.command('chat_prompt', {'text': 'error'})
    wait_for(lambda: w.state(failed) == 'error', 8)
    assert any(e['type'] == 'done' and e.get('receipt') == failed and e['status'] == 'error' for e in w.events())


def test_worker_model_ack_and_image_input_without_transcript_echo(worker):
    w = worker
    changed = w.command('chat_settings', {'model': 'haiku'})
    wait_for(lambda: w.state(changed) == 'completed', 8)
    rejected = w.command('chat_settings', {'model': 'invalid'})
    wait_for(lambda: w.state(rejected) == 'error', 8)
    effort = w.command('chat_settings', {'effort': 'low'})
    wait_for(lambda: w.state(effort) == 'error', 8)
    path = w.root / 'image.png'; raw = b'fixture-not-a-paid-image'; path.write_bytes(raw)
    receipt = w.command('chat_prompt', {'text': 'image', 'attachments': [{'path': str(path), 'mime': 'image/png', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'name': 'image.png'}]})
    wait_for(lambda: w.state(receipt) == 'completed', 8)
    wire = [json.loads(x) for x in (w.root / 'claude-wire.jsonl').read_text().splitlines()]
    image = next(m for m in reversed(wire) if m['type'] == 'user')['message']['content'][1]
    assert image == {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': base64.b64encode(raw).decode()}}
    assert base64.b64encode(raw).decode() not in json.dumps(w.events())


@pytest.mark.parametrize('answer', [True, False, None])
def test_worker_approval_guard_and_single_use(worker, answer):
    w = worker; receipt = w.command('chat_prompt', {'text': 'approve'})
    wait_for(lambda: any(e['type'] == 'approval' and e.get('receipt') == receipt for e in w.events()))
    event = next(e for e in w.events() if e['type'] == 'approval' and e.get('receipt') == receipt)
    guard = hashlib.sha256(json.dumps({'method': event['method'], 'details': event['details']}, sort_keys=True).encode()).hexdigest()
    bad = w.command('chat_answer', {'request_id': event['request_id'], 'answer': True, 'parent_receipt': receipt, 'request_guard': 'invalid'})
    wait_for(lambda: w.state(bad) == 'error')
    assert w.state(receipt) == 'claimed'
    good = w.command('chat_answer', {'request_id': event['request_id'], 'answer': answer, 'parent_receipt': receipt, 'request_guard': guard})
    wait_for(lambda: w.state(receipt) == 'completed')
    assert w.state(good) == 'completed'
    wire = [json.loads(x) for x in (w.root / 'claude-wire.jsonl').read_text().splitlines()]
    replies = [m for m in wire if m['type'] == 'control_response']
    assert len(replies) == 1
    response = replies[0]['response']['response']
    assert response['behavior'] == ('allow' if answer is True else 'deny')
    assert 'updatedPermissions' not in response
    if answer is True: assert response['updatedInput'] == {'command': 'printf fixture-only'}


def test_permission_questions_no_scope_escalation_duplicate_and_cancellation():
    p, sent, events, done = protocol(); p.prompt('turn', {'text': 'question'})
    inputs = {'questions': [{'question': 'target', 'options': [{'label': 'A'}]}, {'question': 'checks', 'multiSelect': True, 'options': [{'label': 'Unit'}, {'label': 'UI'}]}]}
    permission(p, tool='AskUserQuestion', inputs=inputs)
    for bad in ({'target': ['A']}, {'target': ['A', 'B'], 'checks': ['UI']}, {'target': [], 'checks': ['UI']}):
        with pytest.raises(ValueError): p.answer('request', bad)
    p.answer('request', {'target': ['custom'], 'checks': ['Unit', 'UI']})
    answer = sent[-1]['response']['response']
    assert answer['updatedInput']['answers'] == {'target': 'custom', 'checks': 'Unit, UI'}
    assert answer['updatedInput']['questions'] == inputs['questions']
    with pytest.raises(ValueError): p.answer('request', True)
    permission(p, key='cancelled');p.receive({'type': 'control_cancel_request', 'request_id': 'cancelled'})
    with pytest.raises(ValueError): p.answer('cancelled', True)
    permission(p, key='same', inputs={'command': 'original'})
    permission(p, key='same', inputs={'command': 'replaced'})
    p.answer('same', True)
    assert sent[-1]['response']['response']['updatedInput']['command'] == 'original'


def test_unknown_large_and_stale_permissions_fail_closed():
    p, sent, events, done = protocol()
    permission(p)
    assert sent[-1]['response']['response']['behavior'] == 'deny'
    p.prompt('turn', {'text': 'test'})
    permission(p, key='big', inputs={'text': 'a' * 150000})
    assert 'big' not in p.pending and sent[-1]['response']['response']['behavior'] == 'deny'
    p.receive({'type': 'control_request', 'request_id': 'unknown', 'request': {'subtype': 'arbitrary'}})
    assert sent[-1]['response']['subtype'] == 'error'
    permission(p, key='scope')
    with pytest.raises(ValueError): p.answer('scope', {'behavior': 'allow', 'updatedPermissions': ['all']})
    p.interrupt('stop')
    with pytest.raises(ValueError): p.answer('scope', True)


def test_model_timeout_and_interrupt_do_not_claim_early_success():
    p, sent, events, done = protocol()
    p.change_settings('model-receipt', {'model': 'sonnet'})
    assert not done and p.settings_busy and p.settings_deadline > time.monotonic()
    p.receive({'id': sent[-1]['request_id'], 'type': 'response', 'success': False})
    assert done == [('model-receipt', 'error')] and not p.settings_busy
    p.prompt('turn', {'text': 'hold'});p.interrupt('stop')
    key = sent[-1]['request_id']
    p.receive({'type': 'result', 'uuid': 'result-1', 'subtype': 'success', 'result': 'interrupted'})
    assert ('stop', 'completed') not in done and p.settings_busy
    with pytest.raises(ValueError):p.prompt('next', {'text': 'next'})
    p.receive({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': key, 'response': {}}})
    assert ('stop', 'completed') in done and not p.settings_busy
    p.prompt('next', {'text': 'next'})
    p.receive({'type': 'result', 'uuid': 'result-1', 'subtype': 'success'})
    assert p.active == 'next'


def test_manager_claude_start_resume_and_argv_protection(chat):
    obj, project, _ = chat
    executable(Path(project['root']).parent / 'bin' / 'claude')
    sid = uuid.uuid4().hex
    row = obj.action('start', project, {'id': sid, 'cli': 'claude', 'mode': 'chat', 'model': 'sonnet', 'effort': 'max'})
    assert row['provider'] == 'claude'
    native_id = str(uuid.uuid4())
    with obj.connect_db() as db:
        argv = json.loads(db.execute('SELECT argv FROM sessions WHERE id=?', (sid,)).fetchone()[0])
        assert '--permission-prompt-tool' in argv and '--effort' in argv and 'app-server' not in argv
        db.execute("UPDATE sessions SET status='exited',native_thread=?,chat_settings=? WHERE id=?", (native_id, json.dumps({'next': {'model': 'sonnet'}, 'defaults': {'model': '', 'effort': ''}}), sid))
    next_id = uuid.uuid4().hex
    obj.action('start', project, {'id': next_id, 'cli': 'claude', 'mode': 'chat', 'continue_session': sid, 'model': ''})
    with obj.connect_db() as db:
        argv = json.loads(db.execute('SELECT argv FROM sessions WHERE id=?', (next_id,)).fetchone()[0])
    assert argv[-2:] == ['--resume', native_id] and '--model' not in argv
    with pytest.raises(DevError): obj.action('start', project, {'id': uuid.uuid4().hex, 'cli': 'claude', 'mode': 'chat', 'continue_session': sid})
    with pytest.raises((ValueError, DevError)): obj.action('start', project, {'id': uuid.uuid4().hex, 'cli': 'claude', 'mode': 'chat', 'argv': ['--dangerously-skip-permissions']})


@pytest.mark.asyncio
async def test_old_agent_explicitly_rejects_claude_but_retains_other_chat(service):
    obj, project, _ = service
    obj.runtime.connections['device'] = SimpleNamespace(native_protocol=1, native_chat_protocol=2)
    for action in ('start', 'chat_catalog'):
        with pytest.raises(DevError, match='尚不支持 Claude') as error:
            await obj.request(action, project, {'mode': 'chat', 'cli': 'claude'})
        assert error.value.code == 'CLI_CHAT_AGENT_UPDATE_REQUIRED'
