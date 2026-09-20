"""Deterministic native pipe processes: no network, account, or model invocation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from agent.chat_worker import Protocol, image_inputs
from shared.native_cli import database

FAKE = r'''
import json, sys
provider=sys.argv[1]
with open('launch-argv.json','w') as f: json.dump(sys.argv[2:],f)
def emit(value):
    raw=json.dumps(value)+'\n'
    cut=max(1,len(raw)//2)
    sys.stdout.write(raw[:cut]);sys.stdout.flush()
    sys.stdout.write(raw[cut:]);sys.stdout.flush()
def done():
    if provider=='pi': emit({'type':'agent_settled'})
    else: emit({'method':'turn/completed','params':{'turn':{'id':'turn-1','status':'completed'}}})
for line in sys.stdin:
    m=json.loads(line)
    with open('wire.jsonl','a') as f: f.write(json.dumps(m)+'\n')
    method=m.get('type',m.get('method',''))
    if provider=='pi':
        if method=='get_state': emit({'type':'response','id':m['id'],'success':True,'data':{'model':{'id':'native-model','provider':'test'},'thinkingLevel':'low'}})
        elif method=='get_available_models': emit({'type':'response','id':m['id'],'success':True,'data':{'models':[{'id':'native-model','provider':'test'}]}})
        elif method=='get_available_thinking_levels': emit({'type':'response','id':m['id'],'success':True,'data':{'levels':['off','low']}})
        elif method=='prompt':
            emit({'type':'response','id':m['id'],'success':True})
            emit({'type':'message_update','assistantMessageEvent':{'type':'text_delta','delta':'hello '}})
            emit({'type':'message_update','assistantMessageEvent':{'type':'thinking_delta','delta':'thinking'}})
            emit({'type':'tool_execution_start','toolCallId':'tool-1','toolName':'read','args':{'path':'x'}})
            if m['message']=='approve': emit({'type':'extension_ui_request','id':'approval-1','method':'confirm','title':'Allow?'})
            elif m['message']!='hold': done()
        elif method=='abort': done()
        elif method=='extension_ui_response': done()
        elif method in ('set_model','set_thinking_level'): emit({'type':'response','id':m['id'],'success':True,'data':{}})
    else:
        if method=='initialize': emit({'id':m['id'],'result':{}})
        elif method in ('thread/start','thread/resume'): emit({'id':m['id'],'result':{'thread':{'id':m['params'].get('threadId','thread-native')},'model':'native-model','reasoningEffort':'low'}})
        elif method=='model/list': emit({'id':m['id'],'result':{'data':[{'id':'native-model','model':'native-model','supportedReasoningEfforts':[{'reasoningEffort':'low'}]}]}})
        elif method=='turn/start':
            emit({'id':m['id'],'result':{'turn':{'id':'turn-1'}}})
            emit({'method':'item/agentMessage/delta','params':{'delta':'hello ','itemId':'message-1'}})
            emit({'method':'item/reasoning/summaryTextDelta','params':{'delta':'thinking'}})
            emit({'method':'item/started','params':{'item':{'id':'tool-1','type':'commandExecution','command':'ls'}}})
            text=m['params']['input'][0]['text']
            if text=='approve': emit({'id':'approval-1','method':'item/commandExecution/requestApproval','params':{'reason':'Allow?'}})
            elif text!='hold': done()
        elif method=='turn/interrupt':
            emit({'id':m['id'],'result':{}});done()
        elif 'result' in m and m.get('id')=='approval-1': done()
'''


def wait(check, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('Timed out waiting for native fixture')


@pytest.fixture(params=['pi', 'codex'])
def worker(tmp_path, request):
    spool = tmp_path / 'spool'
    home = tmp_path / 'home'
    home.mkdir()
    script = tmp_path / 'fake.py'
    script.write_text(FAKE)
    sid = uuid.uuid4().hex
    db = database(spool)
    now = time.time()
    db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,argv,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
               (sid, 'project', 'device', str(tmp_path), str(tmp_path), request.param, 'chat', 'starting', now, now, json.dumps([sys.executable, str(script), request.param]), 'chat'))
    db.commit()
    process = subprocess.Popen([sys.executable, '-m', 'agent.chat_worker', str(spool), sid], cwd=Path(__file__).resolve().parents[1], env={**os.environ, 'HOME': str(home)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    def events():
        data = b''.join(bytes(r[0]) for r in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset', (sid,)))
        return [json.loads(line) for line in data.splitlines()]
    def command(kind, payload):
        receipt = uuid.uuid4().hex
        db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)', (receipt, sid, kind, json.dumps(payload), time.time()))
        db.commit()
        return receipt
    def state(receipt):
        return db.execute('SELECT state FROM commands WHERE id=?', (receipt,)).fetchone()[0]
    wait(lambda: any(e['type'] == 'settings' for e in events()))
    yield request.param, tmp_path, db, process, events, command, state, sid
    if process.poll() is None:
        command('stop', {})
    process.communicate(timeout=10)
    assert process.returncode == 0
    db.close()


def test_serial_followup_interrupt_and_incremental_offsets(worker):
    provider, root, db, process, events, command, state, sid = worker
    launch_args = json.loads((root / 'launch-argv.json').read_text())
    if provider == 'pi':
        assert launch_args[0] == '--extension'
        assert Path(launch_args[1]).is_file()
        assert 'codepier-shell-timeout-' in launch_args[1]
    else:
        assert launch_args == []
    first = command('chat_prompt', {'text': 'hold'})
    wait(lambda: any(e['type'] == 'delta' for e in events()))
    second = command('chat_prompt', {'text': 'followup'})
    time.sleep(.12)
    assert state(first) == 'claimed'
    assert state(second) == 'queued'
    interrupt = command('chat_interrupt', {})
    wait(lambda: state(second) == 'completed')
    assert state(first) == 'interrupted'
    assert state(interrupt) == 'completed'
    assert [e['receipt'] for e in events() if e['type'] == 'user'] == [first, second]
    assert {'delta', 'reasoning', 'tool', 'done'} <= {e['type'] for e in events()}
    chunks = list(db.execute('SELECT offset,data FROM output WHERE session=? ORDER BY offset', (sid,)))
    offset = 0
    for chunk in chunks:
        assert chunk['offset'] == offset
        assert len(chunk['data']) <= 65536
        offset += len(chunk['data'])
    assert offset == db.execute('SELECT size FROM sessions WHERE id=?', (sid,)).fetchone()[0]
    if provider == 'codex':
        assert db.execute('SELECT native_thread FROM sessions WHERE id=?', (sid,)).fetchone()[0] == 'thread-native'


def test_approval_requires_observed_explicit_answer(worker):
    provider, root, db, process, events, command, state, sid = worker
    receipt = command('chat_prompt', {'text': 'approve'})
    wait(lambda: any(e['type'] == 'approval' for e in events()))
    time.sleep(.1)
    assert state(receipt) == 'claimed'
    wire = [json.loads(x) for x in (root / 'wire.jsonl').read_text().splitlines()]
    assert not any(x.get('type') == 'extension_ui_response' or ('result' in x and x.get('id') == 'approval-1') for x in wire)
    wrong = command('chat_answer', {'request_id': 'not-pending', 'answer': True})
    wait(lambda: state(wrong) == 'error')
    answer = command('chat_answer', {'request_id': 'approval-1', 'answer': False if provider == 'pi' else 'decline'})
    wait(lambda: state(receipt) == 'completed')
    assert state(answer) == 'completed'
    duplicate = command('chat_answer', {'request_id': 'approval-1', 'answer': True if provider == 'pi' else 'accept'})
    wait(lambda: state(duplicate) == 'error')


def test_real_image_payload_and_native_settings(worker):
    provider, root, db, process, events, command, state, sid = worker
    image = root / 'test.png'
    image.write_bytes(b'\x89PNG\r\nfixture-image')
    settings = command('chat_settings', {'model': 'test/native-model' if provider == 'pi' else 'native-model', 'effort': 'low'})
    wait(lambda: state(settings) == 'completed')
    receipt = command('chat_prompt', {'text': 'image', 'attachments': [{'path': str(image), 'mime': 'image/png', 'name': 'test.png'}]})
    wait(lambda: state(receipt) == 'completed')
    wire = [json.loads(x) for x in (root / 'wire.jsonl').read_text().splitlines()]
    if provider == 'pi':
        prompt = next(x for x in wire if x.get('type') == 'prompt')
        import base64
        assert base64.b64decode(prompt['images'][0]['data']) == image.read_bytes()
        assert prompt['images'][0]['mimeType'] == 'image/png'
    else:
        prompt = next(x for x in wire if x.get('method') == 'turn/start')
        assert prompt['params']['input'][1] == {'type': 'localImage', 'path': str(image)}
        assert prompt['params']['model']=='native-model' and prompt['params']['effort']=='low'
        assert not any(x.get('method')=='thread/settings/update' for x in wire)
        assert 'approvalPolicy' not in str(wire)


def test_uncertain_claim_is_never_replayed(tmp_path):
    spool = tmp_path / 'spool'
    db = database(spool)
    sid, receipt = uuid.uuid4().hex, uuid.uuid4().hex
    db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,argv,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
               (sid, 'p', 'd', str(tmp_path), str(tmp_path), 'pi', 'chat', 'starting', 1, 1, json.dumps([sys.executable, '-c', 'raise Exception("must not run")']), 'chat'))
    db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?)', (receipt, sid, 'chat_prompt', '{"text":"never replay"}', 'claimed', 1))
    db.commit()
    process = subprocess.run([sys.executable, '-m', 'agent.chat_worker', str(spool), sid], capture_output=True, timeout=10)
    assert process.returncode == 0
    assert db.execute('SELECT state FROM commands WHERE id=?', (receipt,)).fetchone()[0] == 'uncertain'
    assert not db.execute('SELECT 1 FROM output').fetchone()
    assert 'not replayed' in db.execute('SELECT error FROM sessions').fetchone()[0]
    db.close()


def test_pi_retry_end_does_not_complete_and_native_question_validation():
    sent, events, finished = [], [], []
    p = Protocol('pi', {}, sent.append, lambda k, **v: events.append((k, v)), lambda *v: finished.append(v), lambda t: None)
    p.ready = True
    p.prompt('receipt', {'text': 'hello'})
    p.receive({'type': 'agent_end', 'willRetry': True})
    assert p.active == 'receipt' and not finished
    p.receive({'type': 'extension_ui_request', 'id': 'r', 'method': 'select', 'options': ['yes', 'no']})
    with pytest.raises(ValueError):
        p.answer('r', 'forged')
    p.answer('r', 'no')
    p.receive({'type': 'agent_settled'})
    assert finished == [('receipt', 'completed')]


def test_attachments_reject_symlink(tmp_path):
    target = tmp_path / 'file'
    target.write_text('private')
    link = tmp_path / 'link.png'
    link.symlink_to(target)
    with pytest.raises(ValueError):
        image_inputs([{'path': str(link), 'mime': 'image/png'}], 'pi')


def test_native_settings_redact_headers_and_bound_large_events():
    from agent.chat_worker import bounded_event, MAX_EVENT
    data = bounded_event({'type': 'settings', 'model': {'id': 'm', 'headers': {'Authorization': 'secret'}}, 'models': [{'id': 'm', 'apiKey': 'secret', 'provider': 'p'}]})
    assert b'secret' not in data and b'Authorization' not in data
    assert json.loads(data)['model'] == {'id': 'm'}
    data = bounded_event({'type': 'tool', 'receipt': 'r', 'text': 'x' * 2000000})
    assert len(data) <= MAX_EVENT and json.loads(data)['truncated']
    data = bounded_event({'type': 'approval', 'request_id': 'r', 'details': {'text': 'x' * 2000000}})
    assert len(data) <= MAX_EVENT and json.loads(data)['type'] == 'error'


def test_pi_and_codex_question_answers_are_validated_against_pending_request():
    sent = []
    p = Protocol('codex', {}, sent.append, lambda *a, **k: None, lambda *a: None, lambda t: None)
    p.receive({'id': 7, 'method': 'item/tool/requestUserInput', 'params': {'questions': [{'id': 'question'}]}})
    with pytest.raises(ValueError):
        p.answer(7, {'wrong-id': ['yes']})
    with pytest.raises(ValueError):
        p.answer(7, {'question': 'yes'})
    p.answer(7, {'question': ['yes']})
    assert sent == [{'id': 7, 'result': {'answers': {'question': {'answers': ['yes']}}}}]
