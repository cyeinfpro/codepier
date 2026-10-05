"""Real authenticated HTTP Tasks over existing local fixture exec operations."""
import time
import uuid

import pytest

from shared.mcp_protocol import MODERN, PREFIX, request_headers
from shared.util import atomic_json
from tests.support import running_stack

TASKS = 'io.modelcontextprotocol/tasks'


@pytest.fixture(scope='module')
def task_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('tasks-http')) as s:
        s.stop_agent()
        s.config['shell'] = {'enabled':True, 'projects':['*'], 'command':['/bin/sh','-c']}
        atomic_json(s.config_path,s.config)
        s.start_agent()
        yield s


def modern(s,method,params=None,*,capable=True,token=None,headers=None):
    params = dict(params or {})
    params['_meta'] = {PREFIX+'protocolVersion':MODERN,
        PREFIX+'clientCapabilities':{'extensions':{TASKS:{}}} if capable else {}}
    body = {'jsonrpc':'2.0','id':uuid.uuid4().hex,'method':method,'params':params}
    return s.client.post('/mcp',json=body,headers={
        'Authorization':'Bearer '+(token or s.pat),'Accept':'application/json, text/event-stream',
        **request_headers(body),**(headers or {})})


def launch(s,command,*,capable=True):
    args = {'project':'Imago','command':command,'yield_seconds':0,'idempotency_key':uuid.uuid4().hex}
    return args, modern(s,'tools/call',{'name':'exec','arguments':args},capable=capable).json()['result']


def poll(s,identifier):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        value=modern(s,'tasks/get',{'taskId':identifier}).json()['result']
        assert value['resultType']=='complete'
        if value['status']!='working':
            return value
        time.sleep(.1)
    pytest.fail('Fixture task did not reach terminal state')


def test_tasks_are_per_request_flat_durable_and_reuse_the_original_exec(task_stack):
    s=task_stack
    discovery=modern(s,'server/discover').json()['result']
    assert TASKS in discovery['capabilities']['extensions']
    legacy=s.rpc('initialize',{'protocolVersion':'2025-11-25'}).json()['result']
    assert TASKS not in legacy['capabilities']['extensions']
    args,seed=launch(s,'sleep 1; printf standard-task')
    assert seed['resultType']=='task' and 'task' not in seed and seed['status']=='working'
    identifier=seed['taskId']
    retry=modern(s,'tools/call',{'name':'exec','arguments':args}).json()['result']
    assert retry['taskId']==identifier
    no_cap=modern(s,'tasks/get',{'taskId':identifier},capable=False)
    assert no_cap.json()['error']['code']==-32021
    wrong_header=modern(s,'tasks/get',{'taskId':identifier},headers={'Mcp-Name':'b'*32})
    assert wrong_header.json()['error']['code']==-32020
    completed=poll(s,identifier)
    assert completed['status']=='completed'
    assert completed['result']['structuredContent']['output']=='standard-task'
    assert not {'cwd','shell','command'} & completed['result']['structuredContent'].keys()
    assert s.poll(identifier)['result']['data']['cwd']==str(s.imago)
    assert not completed['result']['isError']
    _,plain=launch(s,'sleep .1; printf ordinary',capable=False)
    assert plain['resultType']=='complete' and 'taskId' not in plain
    plain_op=plain['structuredContent']['operation_id']
    assert modern(s,'tasks/get',{'taskId':plain_op}).json().get('error')


def test_task_errors_update_cancel_and_other_connection_boundaries(task_stack):
    s=task_stack
    _,seed=launch(s,'sleep .2; exit 7')
    failed=poll(s,seed['taskId'])
    assert failed['status']=='completed' and failed['result']['isError']
    assert failed['result']['structuredContent']['exit_code']==7
    _,seed=launch(s,'sleep 5; printf should-not-run')
    identifier=seed['taskId']
    other=s.must(s.client.post('/api/grants',json={'label':'other tasks connection',
        'scopes':['read','write','execute'],'projects':[s.project['id']],'days':1}))
    for method in ('tasks/get','tasks/cancel','tasks/update'):
        params={'taskId':identifier}
        if method=='tasks/update':params['inputResponses']={'fake':{'result':{'approve':True}}}
        assert modern(s,method,params,token=other['token']).json().get('error')
    update=modern(s,'tasks/update',{'taskId':identifier,'inputResponses':{'unissued':{'result':{'approve':True}}}}).json()['result']
    assert set(update)=={'resultType','_meta'}
    cancel=modern(s,'tasks/cancel',{'taskId':identifier}).json()['result']
    assert set(cancel)=={'resultType','_meta'}
    assert poll(s,identifier)['status'] in {'cancelled','completed'}
    for method in ('tasks/list','tasks/result'):
        assert modern(s,method).json()['error']['code']==-32601
    s.client.delete('/api/grants/'+other['grant_id'])


def test_legacy_calls_never_materialize_standard_tasks(task_stack):
    s=task_stack
    result=s.mcp('exec',{'project':'Imago','command':'printf legacy','yield_seconds':0,'idempotency_key':uuid.uuid4().hex})
    assert 'resultType' not in result and 'taskId' not in result
    assert s.rpc('tasks/get',{'taskId':'a'*32}).json()['error']['code']==-32601
