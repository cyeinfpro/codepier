"""Fault-oriented tests. All targets are temporary owner-controlled projects.

Network send failures are injected locally; integration tests use real Hub and
Agent processes with abrupt Hub crashes and persisted operation receipts.
"""
from __future__ import annotations
import asyncio
import copy
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from agent.config import validate_config
from agent.journal import Journal
from agent.runner import Agent
from hub.runtime import Runtime, Principal
from hub.store import Store, SCHEMA
from scripts.mcp_stdio_bridge import forward, prepare_request, REMOTE_TOOLS
from shared.contracts import TOOLS
from shared.crypto import digest, token
from shared.util import atomic_json, DevError
from tests.test_unit import engine
from tests.support import wait_for


def key():
    return uuid.uuid4().hex


@pytest.fixture
def local_agent(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    config = tmp_path / 'config.json'
    atomic_json(config, {'hub_url':'127.0.0.1:9', 'device_id':'fixture', 'secret':token(32),
        'state_dir':str(tmp_path/'state'), 'allowed_roots':[{'path':str(root),'writable':True,'allow_tasks':True}], 'tasks':{}})
    a = Agent(config)
    yield a, root
    a.journal.db.close(); a.instance_lock.close()


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path/'hub')
    from tests.legacy_iam_fixture import seed_owner
    seed_owner(store,'owner','admin')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','home',?,?)", (store.encrypt(token()),time.time()))
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) VALUES ('proj','Imago','imago','dev','/tmp/fixture','','write',1,?)", (time.time(),))
    r = Runtime(store); r.wait_seconds = 0
    principal = Principal('panel:admin','owner',{'read','write','execute'},['*'],admin=True)
    yield r, principal
    store.close()


class Peer:
    def __init__(self):
        self.last_seen = time.time(); self.journal_id = 'journal-one'; self.unusable = False
        self.packets = []; self.gate = None
    async def send(self, packet):
        self.packets.append(packet)
        if self.gate: await self.gate.wait()


def decoded(op):
    return json.loads(op['result'])


@pytest.mark.parametrize('value',[None, [], 'text', {}, {'hub_url':None}, {'hub_url':4}])
def test_invalid_config_is_controlled_error(tmp_path,value):
    with pytest.raises(ValueError): validate_config(value,tmp_path/'config.json')


@pytest.mark.parametrize('field,value',[('allowed_roots',[{'path':'relative'}]),('allowed_roots',[{'path':'/tmp','writable':'false'}]),('tasks',{'bad':{'command':'pytest'}}),('tasks',{'bad':{'command':['python'],'timeout':0}}),('tasks',{'bad':{'command':['python'],'projects':'*'}}),('tasks',{'bad':{'command':['python'],'env':{'X':1}}})])
def test_bad_hot_config_does_not_replace_good_config(local_agent,field,value):
    a,_=local_agent; good=copy.deepcopy(a.config); bad=copy.deepcopy(good); bad[field]=value
    atomic_json(a.config_path,bad)
    with pytest.raises(ValueError): a.load_config()
    assert a.config == good


def test_task_read_concurrency_flag_is_boolean(local_agent):
    a,_=local_agent
    config = copy.deepcopy(a.config)
    config["tasks"] = {"check": {"command": ["python"], "allow_read_concurrency": True}}
    assert validate_config(config, a.config_path)["tasks"]["check"]["allow_read_concurrency"] is True
    config["tasks"]["check"]["allow_read_concurrency"] = "yes"
    with pytest.raises(ValueError): validate_config(config, a.config_path)


def test_bridge_covers_exact_remote_catalog():
    assert REMOTE_TOOLS == {name for name,t in TOOLS.items() if not t.local}


@pytest.mark.parametrize('fault',['connect','timeout',408,429,500,502,503,504,'garbled'])
def test_bridge_network_retry_preserves_request_and_operation_key(fault):
    attempts=[]
    req=prepare_request({'jsonrpc':'2.0','id':77,'method':'tools/call','params':{'name':'fs_write','arguments':{'project':'Imago','path':'x','content':'abc','expected_sha256':'new'}}})
    def handler(request):
        attempts.append(json.loads(request.content))
        if len(attempts)==1:
            if fault=='connect': raise httpx.ConnectError('local injected connection drop',request=request)
            if fault=='timeout': raise httpx.ReadTimeout('reply lost',request=request)
            if fault=='garbled': return httpx.Response(200,content=b'partial-json')
            return httpx.Response(fault,headers={'Retry-After':'0'})
        return httpx.Response(200,json={'jsonrpc':'2.0','id':77,'result':{'isError':False}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert forward(c,'http://fixture',req,{},sleep=lambda _:None)['result']['isError'] is False
    assert len(attempts)==2 and attempts[0]==attempts[1]
    assert attempts[0]['params']['arguments']['idempotency_key'].startswith('bridge-')


@pytest.mark.parametrize('status',[400,401,403,404,413])
def test_bridge_does_not_retry_permanent_http_error(status):
    calls=[]
    def handler(req): calls.append(req); return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError): forward(c,'http://fixture',{'id':1},{},sleep=lambda _:None)
    assert len(calls)==1


def test_bridge_does_not_retry_real_tool_failure_or_change_existing_key():
    req={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'fs_edit','arguments':{'idempotency_key':'owner-existing-key'}}}
    prepared=prepare_request(req); assert prepared==req and prepared is not req
    calls=[]
    def handler(r):
        calls.append(r);return httpx.Response(200,json={'jsonrpc':'2.0','id':1,'result':{'isError':True,'structuredContent':{'error':{'code':'SHA_CONFLICT'}}}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c: assert forward(c,'http://fixture',prepared,{})['result']['isError']
    assert len(calls)==1


def test_bridge_retries_are_bounded():
    calls=[]
    def handler(r): calls.append(r);return httpx.Response(503)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError): forward(c,'http://fixture',{'id':1},{},sleep=lambda _:None)
    assert len(calls)==6


@pytest.mark.asyncio
async def test_offline_receipt_is_durable_and_payload_not_public(runtime):
    r,p=runtime; k=key(); args={'project':'Imago','path':'new.py','content':'private-source-marker','expected_sha256':'new','idempotency_key':k}
    receipt=await r.invoke('fs_write',args,p)
    assert receipt['pending'] and receipt['state']=='queued'
    stored=r.store.one('SELECT * FROM operations WHERE id=?',(receipt['operation_id'],))
    assert stored['payload'] and 'private-source-marker' not in stored['payload']+stored['args_summary']
    public=r.operation(receipt['operation_id'],p)
    assert not {'payload','fingerprint','journal_id','idem'} & public.keys()
    assert json.loads(r.store.decrypt(stored['payload']))['args']['content']=='private-source-marker'
    repeated=await r.invoke('fs_write',args,p);assert repeated['operation_id']==receipt['operation_id']
    assert r.store.one('SELECT count(*) AS n FROM operations')['n']==1
    with pytest.raises(DevError,match='幂等键'): await r.invoke('fs_write',{**args,'content':'different'},p)


@pytest.mark.asyncio
async def test_queue_expiry_never_executes(runtime):
    r,p=runtime;op=await r.invoke('tasks_run',{'project':'Imago','task':'pytest','idempotency_key':key()},p)
    r.store.execute('UPDATE operations SET deadline=? WHERE id=?',(time.time()-1,op['operation_id']))
    peer=Peer();r.connections['dev']=peer;await r.deliver(op['operation_id'])
    result=r.operation(op['operation_id'],p)
    assert result['state']=='failed' and result['result']['error']['code']=='QUEUE_EXPIRED' and not peer.packets


@pytest.mark.asyncio
async def test_queued_cancellation_does_not_touch_files(runtime):
    r,p=runtime;op=await r.invoke('fs_write',{'project':'Imago','path':'new','expected_sha256':'new','content':'x','idempotency_key':key()},p)
    await r.cancel(op['operation_id'],p)
    peer=Peer();r.connections['dev']=peer;await r.deliver(op['operation_id'])
    assert r.operation(op['operation_id'],p)['state']=='cancelled' and not peer.packets


@pytest.mark.asyncio
async def test_mapping_change_prevents_queued_execution(runtime):
    r,p=runtime;op=await r.invoke('fs_read',{'project':'Imago','path':'README.md'},p)
    r.store.execute("UPDATE projects SET root='/tmp/different' WHERE id='proj'")
    peer=Peer();r.connections['dev']=peer;await r.deliver(op['operation_id'])
    assert r.operation(op['operation_id'],p)['result']['error']['code']=='AUTHORIZATION_CHANGED' and not peer.packets


@pytest.mark.asyncio
async def test_journal_epoch_change_requires_review_not_reexecution(runtime):
    r,p=runtime;op=await r.invoke('tasks_run',{'project':'Imago','task':'pytest','idempotency_key':key()},p)
    peer=Peer();r.connections['dev']=peer;await r.deliver(op['operation_id'])
    assert peer.packets[0]['type']=='call'
    peer.journal_id='a-new-empty-agent-state';peer.packets.clear();await r.deliver(op['operation_id'])
    final=r.operation(op['operation_id'],p)
    assert final['state']=='needs_review' and not peer.packets
    assert final['result']['error']['code']=='JOURNAL_CHANGED'


@pytest.mark.asyncio
async def test_corrupt_recovery_payload_stops_without_fake_success(runtime):
    r,p=runtime;op=await r.invoke('fs_read',{'project':'Imago','path':'README.md'},p)
    r.store.execute("UPDATE operations SET payload='invalid' WHERE id=?",(op['operation_id'],))
    await r.deliver(op['operation_id'])
    assert r.operation(op['operation_id'],p)['state']=='needs_review'


@pytest.mark.asyncio
async def test_slow_socket_does_not_lock_http_admission(runtime):
    r,p=runtime;one=await r.invoke('fs_read',{'project':'Imago','path':'one'},p)
    peer=Peer();peer.gate=asyncio.Event();r.connections['dev']=peer
    send=asyncio.create_task(r.deliver(one['operation_id']))
    await asyncio.sleep(.02);assert peer.packets
    two=await asyncio.wait_for(r.invoke('fs_read',{'project':'Imago','path':'two'},p),.3)
    assert two['pending'] and two['operation_id']!=one['operation_id']
    peer.gate.set();await send


@pytest.mark.asyncio
async def test_duplicate_delivery_keeps_id_and_accepted_only_probes(runtime):
    r,p=runtime;op=await r.invoke('tasks_run',{'project':'Imago','task':'pytest','idempotency_key':key()},p)
    peer=Peer();r.connections['dev']=peer
    await r.deliver(op['operation_id']);await r.deliver(op['operation_id'])
    assert len(peer.packets)==2 and peer.packets[0]==peer.packets[1]
    r.store.execute('UPDATE operations SET accepted_at=? WHERE id=?',(time.time(),op['operation_id']))
    await r.deliver(op['operation_id']);assert peer.packets[-1]=={'type':'probe','id':op['operation_id']}


@pytest.mark.asyncio
async def test_operations_list_recovers_lost_response_by_key(runtime):
    r,p=runtime;k=key();op=await r.invoke('fs_read',{'project':'Imago','path':'README.md','idempotency_key':k},p)
    found=await r.invoke('operations_list',{'project':'Imago','idempotency_key':k},p)
    assert [x['id'] for x in found['operations']]==[op['operation_id']]
    status=await r.invoke('operations_wait',{'operation_id':op['operation_id'],'wait_seconds':0},p)
    assert status['pending'] and status['next']=='operations_wait'


@pytest.mark.asyncio
async def test_final_result_erases_queue_payload_and_duplicate_result_ignored(runtime):
    r,p=runtime;op=await r.invoke('fs_read',{'project':'Imago','path':'README.md'},p)
    row=r.store.one('SELECT * FROM operations WHERE id=?',(op['operation_id'],))
    r.complete(row,{'ok':True,'data':{'content':'one'}})
    final=r.store.one('SELECT * FROM operations WHERE id=?',(op['operation_id'],));assert final['payload'] is None
    r.complete(final,{'ok':True,'data':{'content':'two'}})
    assert r.operation(op['operation_id'],p)['result']['data']['content']=='one'

@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code,timed_out,expected',[(2,False,'退出码 2'),(-9,True,'超时')])
async def test_failed_task_has_summary_without_losing_output(runtime,exit_code,timed_out,expected):
    r,p=runtime
    op=await r.invoke('tasks_run',{'project':'Imago','task':'test','idempotency_key':key()},p)
    row=r.store.one('SELECT * FROM operations WHERE id=?',(op['operation_id'],))
    payload={'ok':True,'data':{'exit_code':exit_code,'timed_out':timed_out,'output':'test failure details'}}
    r.complete(row,payload)
    result=r.operation(op['operation_id'],p)
    assert result['state']=='failed' and expected in result['error']
    assert result['output']=='test failure details' and result['result']['ok'] is True
    # New diagnostic fields must not alter the original Agent result fields.
    assert all(result['result']['data'][k] == v for k, v in payload['data'].items())
    assert result['result']['data']['command_ok'] is False
    assert 'command_ok' not in payload['data']  # complete() must not mutate its caller.


def test_journal_accepted_resumes_but_started_mutation_does_not(tmp_path):
    j=Journal(tmp_path/'state');identity=j.journal_id
    mutation={'tool':'tasks_run','args':{'task':'pytest'}}
    read={'tool':'fs_read','args':{'path':'README.md'}}
    j.start('accepted',mutation);j.start('started',mutation);j.mark_running('started')
    j.start('read',read);j.mark_running('read');j.cancel('future');j.db.close()
    recovered=Journal(tmp_path/'state')
    try:
        assert recovered.journal_id==identity
        assert recovered.status('accepted')['status']=='retryable'
        assert recovered.start('accepted',mutation) is None
        assert recovered.status('read')['status']=='retryable'
        assert recovered.status('started')['result']['error']['code']=='INTERRUPTED'
        assert recovered.start('started',mutation)['error']['code']=='INTERRUPTED'
        assert recovered.is_cancelled('future')
    finally: recovered.db.close()


def test_log_snapshot_is_monotonic_and_survives_journal_restart(tmp_path):
    j=Journal(tmp_path/'state');j.start('task',{'tool':'tasks_run'})
    j.update_output('task','完整一\n',1);j.update_output('task','完整二\n',2);j.update_output('task','重复旧数据',1)
    assert j.status('task')['output']=='完整二\n';j.db.close()
    j=Journal(tmp_path/'state')
    try: assert j.status('task')['output_seq']==2 and j.status('task')['output']=='完整二\n'
    finally: j.db.close()


@pytest.mark.asyncio
async def test_stdout_draining_never_waits_for_network(local_agent):
    a,root=local_agent
    async def blackhole(_): await asyncio.sleep(60)
    a.send=blackhole
    a.journal.start('large-log',{'tool':'tasks_run'});a.journal.mark_running('large-log')
    code='import os; os.write(1,b"x"*4_000_000); print("\\nFINISHED")'
    result=await asyncio.wait_for(a.run_process('large-log',[sys.executable,'-u','-c',code],root,10,stream=True),5)
    assert result['exit_code']==0 and 'FINISHED' in result['output'] and result['output_truncated']
    assert len(a.pending_output)==1  # bounded snapshot, not one packet per output chunk


@pytest.mark.asyncio
async def test_utf8_split_output_and_local_task_environment(local_agent):
    a,root=local_agent;a.journal.start('unicode',{'tool':'tasks_run'})
    code='import os,time; b="测试通过✓".encode(); [(os.write(1,bytes([x])),time.sleep(.002)) for x in b]; print(os.environ.get("LOCAL_TASK_TEST","missing"))'
    result=await a.run_process('unicode',[sys.executable,'-u','-c',code],root,5,stream=True,task_env={'LOCAL_TASK_TEST':'environment-ok'})
    assert result['exit_code']==0 and '测试通过✓environment-ok' in result['output'] and '�' not in result['output']


@pytest.mark.asyncio
async def test_deadline_expired_at_agent_before_start(local_agent):
    a,root=local_agent
    await a.handle({'id':'expired','tool':'fs_write','project':{'root':str(root),'mode':'write','alias':'Imago'},'args':{'project':'Imago','path':'never.txt','content':'x','expected_sha256':'new','idempotency_key':key()},'not_after':time.time()-1})
    assert not (root/'never.txt').exists()
    assert a.journal.status('expired')['result']['error']['code']=='QUEUE_EXPIRED'


@pytest.mark.asyncio
async def test_duplicate_agent_requests_execute_one_write(local_agent):
    a,root=local_agent
    request={'id':'repeat','tool':'fs_write','project':{'root':str(root),'mode':'write','alias':'Imago'},'args':{'project':'Imago','path':'once.txt','content':'once','expected_sha256':'new','idempotency_key':key()},'not_after':time.time()+60}
    await asyncio.gather(*(a.handle(copy.deepcopy(request)) for _ in range(10)))
    assert (root/'once.txt').read_text()=='once'
    assert len(a.journal.history(str(root),'once.txt'))==1


@pytest.mark.asyncio
async def test_project_queue_does_not_starve_other_projects(local_agent):
    a,root=local_agent; other=root/'Other';other.mkdir();(other/'README.md').write_text('other project')
    first_lock=asyncio.Lock();await first_lock.acquire();a.project_locks[str(root)]=first_lock
    tasks=[]
    try:
        for i in range(6):
            tasks.append(asyncio.create_task(a.handle({'id':f'blocked-{i}','tool':'fs_read','project':{'root':str(root),'alias':'Imago'},'args':{'project':'Imago','path':'README.md'}})))
        await asyncio.sleep(.02)
        await asyncio.wait_for(a.handle({'id':'free','tool':'fs_read','project':{'root':str(other),'alias':'Nexus'},'args':{'project':'Nexus','path':'README.md'}}),1)
        assert a.journal.status('free')['result']['data']['content']=='other project'
    finally:
        first_lock.release();await asyncio.gather(*tasks)


def test_read_many_response_budget_reports_remaining_paths(engine):
    e,p,root=engine
    paths=[]
    for i in range(6):
        name=f'large-{i}.txt';(root/name).write_text('a'*900_000);paths.append(name)
    result=e.call('fs_read_many',p,{'paths':paths,'max_lines':1000})
    assert result['truncated'] and result['remaining_paths']
    assert len(result['files'])+len(result['remaining_paths'])==len(paths)
    assert len(json.dumps(result).encode())<2*1024*1024+4096


def test_new_file_publish_does_not_overwrite_concurrent_creation(engine,monkeypatch):
    e,p,root=engine;actual_link=os.link
    def race(source,dest,*args,**kwargs):
        Path(dest).write_text('local editor won')
        return actual_link(source,dest,*args,**kwargs)
    monkeypatch.setattr(os,'link',race)
    with pytest.raises(DevError,match='文件'): e.mutate(p,'race.txt','new',b'AI overwrite')
    assert (root/'race.txt').read_text()=='local editor won'


def test_v1_store_migration_retains_key_and_existing_records(tmp_path):
    directory=tmp_path/'hub';directory.mkdir();db=sqlite3.connect(directory/'hub.sqlite3');db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('schema','1')")
    db.execute("INSERT INTO audit(at,actor,action,status,detail) VALUES (1,'owner','existing-audit','ok','{}')");db.commit();db.close()
    store=Store(directory);secret=store.encrypt('persistent-key-test');keybytes=(directory/'master.key').read_bytes()
    assert store.one("SELECT value FROM meta WHERE key='schema'")['value']=='10'
    assert store.one('SELECT action FROM audit')['action']=='existing-audit'
    assert {'payload','journal_id','accepted_at','cancel_requested','output_seq'}.issubset({r['name'] for r in store.all('PRAGMA table_info(operations)')})
    store.close();store=Store(directory)
    try: assert store.decrypt(secret)=='persistent-key-test' and (directory/'master.key').read_bytes()==keybytes
    finally: store.close()


def test_outbox_recovers_in_bounded_batches(tmp_path):
    j=Journal(tmp_path/'state')
    try:
        for i in range(10):
            j.start(str(i),{'tool':'fs_read'});j.finish(str(i),{'ok':True,'data':{'content':'a'*900_000}})
        first=j.outbox()
        assert 1 <= len(first) <= 4
        assert len(json.dumps(first)) < 4*1024*1024
        for item in first:j.ack(item['id'])
        assert not {x['id'] for x in first} & {x['id'] for x in j.outbox()}
    finally:j.db.close()


def test_late_local_edit_during_temp_flush_is_not_overwritten(engine,monkeypatch):
    e,p,root=engine;path=root/'src/app.py';sha=digest(path.read_bytes());real_chmod=os.chmod
    def change_during_flush(target,mode,*args,**kwargs):
        if Path(target).name.startswith('.rd-'):
            path.write_text('saved by local IDE while temp file was written')
        return real_chmod(target,mode,*args,**kwargs)
    monkeypatch.setattr(os,'chmod',change_during_flush)
    with pytest.raises(DevError):e.mutate(p,'src/app.py',sha,b'AI stale content')
    assert path.read_text()=='saved by local IDE while temp file was written'
