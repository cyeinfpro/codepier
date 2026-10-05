"""Tasks keep original exec authority, terminal state and legacy wire behavior."""
import asyncio
from dataclasses import replace
import json
import time
import uuid

import pytest

from hub.mcp_tasks import TaskService, CreatedTask, EXTENSION, arguments
from hub.runtime import Runtime, Principal
from hub.store import Store
from shared.crypto import token
from shared.mcp_protocol import PREFIX, MODERN, complete, request_headers, validate_modern, ProtocolError
from shared.util import DevError
from tests.legacy_iam_fixture import seed_owner, seed_grant


@pytest.fixture
def task_runtime(tmp_path):
    store = Store(tmp_path / 'hub')
    seed_owner(store, 'owner', 'admin')
    seed_owner(store, 'other', 'other')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,?)", (store.encrypt(token()), time.time()))
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created) VALUES ('proj','Fixture','fixture','dev','/tmp/fixture','write',1,?)", (time.time(),))
    seed_grant(store, 'grant', 'owner', scopes=('read','write','execute'), projects=('proj',))
    seed_grant(store, 'second', 'owner', scopes=('read','write','execute'), projects=('proj',))
    runtime = Runtime(store)
    runtime.wait_seconds = 0
    principal = Principal('mcp:grant:fixture', 'owner', {'read','write','execute'}, ['proj'], grant_id='grant')
    yield runtime, principal, TaskService(runtime)
    store.close()


async def create_task(runtime, principal, tasks):
    args = {'project':'Fixture', 'command':'printf fixture', 'yield_seconds':0, 'idempotency_key':uuid.uuid4().hex}
    value = await runtime.invoke('exec', args, principal)
    task = tasks.create(value, args, principal)
    assert isinstance(task, CreatedTask)
    return task.body['taskId'], args, value


def terminal(runtime, identifier, state='succeeded', output='done'):
    result = {'ok':True, 'data':{'exit_code':0 if state=='succeeded' else 7, 'output':output, 'command_ok':state=='succeeded'}}
    runtime.store.execute('UPDATE operations SET state=?,result=?,updated=? WHERE id=?',
                          (state,json.dumps(result),time.time(),identifier))


@pytest.mark.asyncio
async def test_durable_task_creation_and_retry_use_original_operation(task_runtime):
    runtime, principal, tasks = task_runtime
    identifier, args, value = await create_task(runtime, principal, tasks)
    retry = await runtime.invoke('exec', args, principal)
    assert tasks.create(retry,args,principal).body['taskId'] == identifier
    assert len(runtime.store.all('SELECT * FROM operations')) == 1
    assert len(runtime.store.all('SELECT * FROM mcp_tasks')) == 1
    assert tasks.get(identifier,principal)['status'] == 'working'
    with pytest.raises(DevError):
        tasks.create(value,{**args,'idempotency_key':'different-key'},principal)
    assert tasks.create({'operation_id':identifier},args,principal) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['succeeded','failed','needs_review','interrupted'])
async def test_terminal_state_is_latched_before_poll_and_never_regresses(task_runtime, state):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime,principal,tasks)
    terminal(runtime,identifier,state)
    runtime.store.execute("UPDATE operations SET state='reconnecting',result=NULL WHERE id=?", (identifier,))
    frozen = tasks.get(identifier,principal)
    assert frozen['status'] == 'completed'
    assert frozen['result']['isError'] is (state != 'succeeded')
    assert frozen['result']['structuredContent']['output'] == 'done'
    terminal(runtime,identifier,output='late recovery must not replace')
    assert tasks.get(identifier,principal) == frozen


@pytest.mark.asyncio
async def test_tool_failure_is_completed_and_cancellation_is_cooperative(task_runtime, monkeypatch):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime,principal,tasks)
    async def cancel(identifier, user):
        runtime.store.execute("UPDATE operations SET cancel_requested=1,state='cancelling' WHERE id=?", (identifier,))
    monkeypatch.setattr(runtime,'cancel',cancel)
    assert await tasks.call('tasks/cancel',identifier,principal) == {}
    assert tasks.get(identifier,principal)['status'] == 'working'
    terminal(runtime,identifier,'failed')
    assert tasks.get(identifier,principal)['status'] == 'completed'
    assert tasks.get(identifier,principal)['result']['isError']
    other, _, _ = await create_task(runtime,principal,tasks)
    terminal(runtime,other,'cancelled')
    assert tasks.get(other,principal)['status'] == 'cancelled'
    assert 'result' not in tasks.get(other,principal)


@pytest.mark.asyncio
async def test_exact_grant_owner_scope_and_revocation_are_rechecked(task_runtime):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime,principal,tasks)
    for caller in [replace(principal,grant_id='second',actor='mcp:second:fixture'),
                   replace(principal,user_id='other'), replace(principal,space_id='different')]:
        with pytest.raises(DevError):
            tasks.get(identifier,caller)
    terminal(runtime,identifier)
    runtime.store.execute("UPDATE grants SET scopes='[\"read\"]' WHERE id='grant'")
    with pytest.raises(DevError):
        tasks.get(identifier,principal)
    runtime.store.execute("UPDATE grants SET revoked=1 WHERE id='grant'")
    with pytest.raises(DevError):
        tasks.get(identifier,principal)


@pytest.mark.asyncio
async def test_revocation_during_cancel_does_not_release_result(task_runtime, monkeypatch):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime,principal,tasks)
    async def cancel(identifier, user):
        await asyncio.sleep(0)
        runtime.store.execute("UPDATE grants SET revoked=1 WHERE id='grant'")
    monkeypatch.setattr(runtime,'cancel',cancel)
    with pytest.raises(DevError):
        await tasks.call('tasks/cancel',identifier,principal)


@pytest.mark.asyncio
async def test_update_unknown_inputs_is_authorized_noop_and_terminal_cancel_noop(task_runtime, monkeypatch):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime,principal,tasks)
    assert await tasks.call('tasks/update',identifier,principal) == {}
    terminal(runtime,identifier)
    async def denied(*_):
        pytest.fail('A terminal task must not cancel a recovered operation')
    monkeypatch.setattr(runtime,'cancel',denied)
    assert await tasks.call('tasks/cancel',identifier,principal) == {}
    assert len(runtime.store.all('SELECT * FROM operations')) == 1


@pytest.mark.parametrize('method',['tasks/get','tasks/update','tasks/cancel'])
def test_task_routing_headers_and_trusted_result_discriminator(method):
    params = {'taskId':'a'*32,'_meta':{PREFIX+'protocolVersion':MODERN,PREFIX+'clientCapabilities':{'extensions':{EXTENSION:{}}}}}
    if method == 'tasks/update':
        params['inputResponses'] = {'never-issued': {'result': {'approved':True}}}
    body = {'jsonrpc':'2.0','id':1,'method':method,'params':params}
    headers={k.lower():v for k,v in request_headers(body).items()}
    assert headers['mcp-name'] == 'a'*32
    validate_modern(body,headers)
    arguments(method,params)
    with pytest.raises(ProtocolError):
        validate_modern(body,{**headers,'mcp-name':'b'*32})
    assert complete({'resultType':'task','taskId':'injected'})['resultType'] == 'complete'


@pytest.mark.parametrize('method,params', [
    ('tasks/get',{'taskId':'bad'}),
    ('tasks/get',{'taskId':'a'*32,'extra':True}),
    ('tasks/update',{'taskId':'a'*32}),
    ('tasks/update',{'taskId':'a'*32,'inputResponses':[]}),
])
def test_task_parameters_are_bounded(method,params):
    with pytest.raises(ProtocolError):
        arguments(method,params)


@pytest.mark.asyncio
async def test_only_a_real_protocol_failure_produces_failed_task(task_runtime):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime,principal,tasks)
    runtime.store.execute("UPDATE operations SET state='succeeded',result='[]',updated=? WHERE id=?",
                          (time.time(),identifier))
    value=tasks.get(identifier,principal)
    assert value['status']=='failed' and value['error']['code']==-32603
    assert 'result' not in value
    runtime.store.execute("UPDATE operations SET state='running',result=NULL WHERE id=?", (identifier,))
    assert tasks.get(identifier,principal)==value


@pytest.mark.asyncio
async def test_task_snapshot_survives_store_reopen(task_runtime):
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime,principal,tasks)
    terminal(runtime,identifier)
    expected=tasks.get(identifier,principal)
    second=Store(runtime.store.directory)
    try:
        restored=TaskService(Runtime(second))
        assert restored.get(identifier,principal)==expected
    finally:
        second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['success','remote-error','expired-media'])
async def test_frozen_renderer_equals_authorized_synchronous_exec(task_runtime,kind):
    from copy import deepcopy
    from hub.core_tools import result as core_result
    runtime, principal, tasks = task_runtime
    identifier, _, _ = await create_task(runtime,principal,tasks)
    saved={'ok':True,'data':{'exit_code':0,'output':'x'*9000,'command_ok':True}}
    if kind=='remote-error':
        saved={'ok':False,'error':{'code':'FIXTURE_FAILURE','message':'safe message',
               'password':'must-not-disclose','next':'computer_observe','input_sent':False}}
    if kind=='expired-media':
        saved['data'].update(computer_expires_at=0,text='expired private text',
                             _computer_content=[{'type':'text','text':'expired'}],url='https://private.invalid')
    try:
        expected=core_result('exec',{},runtime.authorized_result(identifier,deepcopy(saved),principal))
    except DevError as exc:
        value={'error':{'code':exc.code,'message':exc.message,**exc.details}}
        expected={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],
                  'structuredContent':value,'isError':True}
    runtime.store.execute("UPDATE operations SET state=?,result=?,updated=? WHERE id=?",
        ('failed' if kind=='remote-error' else 'succeeded',json.dumps(saved),time.time(),identifier))
    frozen=runtime.store.one('SELECT terminal_operation FROM mcp_tasks WHERE task_id=?',(identifier,))['terminal_operation']
    assert tasks.get(identifier,principal)['result']==expected
    assert runtime.store.one('SELECT terminal_operation FROM mcp_tasks WHERE task_id=?',(identifier,))['terminal_operation']==frozen
    assert 'must-not-disclose' not in json.dumps(expected)
    if kind=='expired-media':
        assert 'expired private text' not in json.dumps(expected)
