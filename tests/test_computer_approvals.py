import asyncio
import json
import sys
import time
from types import SimpleNamespace
import pytest
from agent.computer_approvals import AgentApprovals
from agent.computer_appserver import AppServerClient
from shared.util import DevError

@pytest.mark.asyncio
@pytest.mark.parametrize('action',['accept','decline','cancel'])
async def test_decision_is_fenced_and_consumed(action):
    sent=[]
    async def send(data):sent.append(data);return True
    approvals=AgentApprovals(send)
    context={'session_id':'s','operation_id':'op','project_id':'p','owner':'grant:g','app':'Fixture'}
    task=asyncio.create_task(approvals.request(context,'Allow?',lambda:True,2))
    await asyncio.sleep(0)
    key=sent[0]['request_id']
    approvals.decide({'request_id':key,'session_id':'wrong','action':'accept'})
    assert not task.done()
    approvals.decide({'request_id':key,'session_id':'s','action':action})
    result=await task
    assert result['action']==action and not approvals.pending
    assert sent[-1]['type']=='computer_approval_closed'
    approvals.decide({'request_id':key,'session_id':'s','action':'accept'})

@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['timeout','revoke','disconnect','stop'])
async def test_pending_consent_fails_closed(reason):
    sent=[];valid=True
    async def send(data):sent.append(data);return True
    approvals=AgentApprovals(send)
    task=asyncio.create_task(approvals.request({'session_id':'s'},'Allow?',lambda:valid,.04 if reason=='timeout' else 2))
    await asyncio.sleep(0)
    if reason=='revoke':valid=False
    elif reason=='disconnect':approvals.cancel()
    elif reason=='stop':approvals.cancel('s')
    assert (await task)['action']=='cancel'
    assert not approvals.pending

@pytest.mark.asyncio
@pytest.mark.parametrize('callback,boot_delay', [
    *[(case, 0) for case in ('matching','wrong-thread','wrong-server','url','fields','unknown')],
    pytest.param('wrong-server', 2.2, id='slow-startup-wrong-server'),
])
async def test_appserver_callbacks_are_not_implicitly_approved(tmp_path,callback,boot_delay):
    script=tmp_path/'fake.py'
    script.write_text('''import sys,json,time
time.sleep(float(sys.argv[2]))
print('FIXTURE_READY',flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if 'method' not in r:continue
 p={'threadId':'thread','serverName':'codepier_computer','mode':'form','message':'Allow Fixture?','requestedSchema':{'type':'object','properties':{}}}
 case=sys.argv[1]
 if case=='wrong-thread':p['threadId']='other'
 if case=='wrong-server':p['serverName']='other'
 if case=='url':p['mode']='url'
 if case=='fields':p['requestedSchema']['properties']={'password':{'type':'string'}}
 print(json.dumps({'id':'approval','method':'other' if case=='unknown' else 'mcpServer/elicitation/request','params':p}),flush=True)
 response=json.loads(sys.stdin.readline())
 print(json.dumps({'id':r['id'],'result':{'reply':response}}),flush=True)
''')
    client=AppServerClient({},2)
    client.thread_id='thread';calls=[]
    async def handler(message):calls.append(message);return {'action':'decline'}
    client.approval_handler=handler
    client.process=await asyncio.create_subprocess_exec(sys.executable,str(script),callback,str(boot_delay),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
    try:
        # Spawn returns before Python/coverage startup has completed. This test
        # measures callback authorization, not cold interpreter startup. Keep
        # the RPC's two-second budget unchanged and bound fixture boot separately.
        assert await asyncio.wait_for(client.process.stdout.readline(),15)==b'FIXTURE_READY\n'
        result=await client.request('mcpServer/tool/call',{})
        reply=result['reply']
        if callback=='matching':assert reply['result']['action']=='decline' and len(calls)==1
        elif callback=='unknown':assert 'error' in reply and not calls
        else:assert reply['result']['action']=='cancel' and not calls
    finally:await client.close()

@pytest.mark.asyncio
async def test_appserver_timeout_does_not_retry(tmp_path):
    script=tmp_path/'blocked.py';script.write_text('import sys,time\nprint("FIXTURE_READY",flush=True)\nsys.stdin.readline()\ntime.sleep(30)\n')
    client=AppServerClient({},.03)
    client.process=await asyncio.create_subprocess_exec(sys.executable,str(script),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
    try:
        assert await asyncio.wait_for(client.process.stdout.readline(),15)==b'FIXTURE_READY\n'
        with pytest.raises(DevError) as error:await client.request('mcpServer/tool/call',{})
        assert error.value.code=='COMPUTER_TIMEOUT' and client.closed and client.process.returncode is not None
    finally:await client.close()

@pytest.mark.asyncio
async def test_hub_codepier_live_operation_and_panel_only_routes(tmp_path):
    from hub.store import Store
    from hub.runtime import Runtime, Principal
    from shared.crypto import password_hash
    import uuid
    store=Store(tmp_path/'hub');rt=Runtime(store)
    store.execute('INSERT INTO users VALUES (?,?,?,?)',('u','admin',password_hash('fixture-password-123'),time.time()))
    store.execute('INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)',('d','Device',store.encrypt('s'),time.time()))
    store.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,created) VALUES (?,?,?,?,?,?)',('p','Test','test','d','/fixture',time.time()))
    request={'project':{'id':'p','alias':'Test','root':'/fixture'}}
    store.execute('INSERT INTO operations(id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)',('op','d','p','panel:admin','computer_observe','{}','f','running',time.time(),time.time(),store.encrypt(json.dumps(request))))
    sent=[]
    async def send(data):sent.append(data)
    connection=SimpleNamespace(unusable=False,device_secret=None,send=send)
    rt.connections['d']=connection
    key=uuid.uuid4().hex
    data={'type':'computer_approval','request_id':key,'session_id':'a'*32,'project_id':'p','owner':'panel:admin','app':'Fixture','operation_id':'op','message':'Allow?','expires_at':time.time()+40}
    inbox=rt.computer_approvals
    inbox.receive('d',connection,{**data,'owner':'grant:other'});assert not inbox.list()
    inbox.receive('d',connection,data);assert len(inbox.list())==1
    principal=Principal('panel:admin','u',{'computer'},['*'],admin=True)
    await inbox.decide(key,'decline',principal)
    assert sent[0]['action']=='decline' and not inbox.list()
    with pytest.raises(DevError):await inbox.decide(key,'accept',principal)
    inbox.receive('d',connection,{**data,'request_id':uuid.uuid4().hex})
    rt.detach_connection('d',connection);assert not inbox.list()
    store.close()
