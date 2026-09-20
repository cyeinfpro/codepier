"""Real-process disruption tests, not a live ChatGPT or Oracle deployment."""
from __future__ import annotations
import concurrent.futures
import copy
import json
import os
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path
import pytest
from hub.store import Store
from shared.util import atomic_json
from tests.support import wait_for


def key(): return uuid.uuid4().hex


def online(s):
    return any(d['id']==s.device and d['online'] for d in s.client.get('/api/devices').json()['devices'])


def abrupt_hub_stop(s):
    s.hub.kill();s.hub.wait(timeout=8)


def restart_hub(s):
    s.start_hub();wait_for(lambda:online(s),timeout=20)


def agent_record(s,id):
    with sqlite3.connect(s.directory/'agent-state'/'agent.sqlite3') as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM calls WHERE id=?',(id,)).fetchone()
        return dict(row) if row else None


def install_task(s,name,command,timeout=25):
    s.config['tasks'][name]={'command':command,'timeout':timeout,'projects':['Imago']}
    atomic_json(s.config_path,s.config)
    wait_for(lambda:any(t['name']==name for t in s.fs('tasks_list')['tasks']),timeout=15)


def test_offline_mcp_read_write_survive_hub_restart(stack):
    s=stack;s.stop_agent();wait_for(lambda:not online(s))
    name='offline-'+key()+'.py';k=key()
    args={'project':'Imago','path':name,'content':'# queued once\n','expected_sha256':'new','idempotency_key':k}
    try:
        write=s.mcp('fs_write',args);assert not write['isError']
        receipt=write['structuredContent'];assert receipt['pending'] and receipt['state']=='queued'
        read=s.mcp('fs_read',{'project':'Imago','path':'README.md','idempotency_key':key()})['structuredContent']
        abrupt_hub_stop(s);s.start_hub()
        recovered=s.mcp('operations_list',{'project':'Imago','idempotency_key':k})['structuredContent']
        assert recovered['operations'][0]['id']==receipt['operation_id']
    finally: s.start_agent()
    assert s.poll(receipt['operation_id'],20)['state']=='succeeded'
    assert '# Imago' in s.poll(read['operation_id'],10)['result']['data']['content']
    again=s.mcp('fs_write',args)['structuredContent'];assert again['operation_id']==receipt['operation_id']
    assert (s.imago/name).read_text()=='# queued once\n'
    assert len(s.fs('history_list',path=name)['backups'])==1


def test_actual_pytest_survives_running_hub_crash_without_restart(stack):
    s=stack;counter='pytest-counter-'+key()+'.txt';name='pytest-real-'+key()[:8]
    code=f'import pathlib,time,os,sys; p=pathlib.Path({counter!r}); p.open("a").write("x"); print("PYTEST_STARTED",flush=True); time.sleep(4); os.execv(sys.executable,[sys.executable,"-m","pytest","-q"])'
    install_task(s,name,[sys.executable,'-u','-c',code])
    k=key();args={'project':'Imago','task':name,'idempotency_key':k}
    receipt=s.mcp('tasks_run',args)['structuredContent']
    wait_for(lambda:(s.imago/counter).exists())
    abrupt_hub_stop(s);time.sleep(.3);restart_hub(s)
    repeated=s.mcp('tasks_run',args)['structuredContent'];assert repeated['operation_id']==receipt['operation_id']
    op=s.poll(receipt['operation_id'],25)
    assert op['state']=='succeeded' and op['result']['data']['exit_code']==0
    assert 'passed' in op['output'] and (s.imago/counter).read_text()=='x'
    assert op['attempts']>=1


def test_completion_while_hub_down_is_replayed_not_reexecuted(stack):
    s=stack;counter='outbox-counter-'+key()+'.txt';name='outbox-'+key()[:8]
    code=f'import pathlib,time; pathlib.Path({counter!r}).open("a").write("x"); print("STARTED",flush=True); time.sleep(1.5); print("测试日志完整 / DONE")'
    install_task(s,name,[sys.executable,'-u','-c',code])
    args={'project':'Imago','task':name,'idempotency_key':key()};receipt=s.call('tasks_run',args)
    wait_for(lambda:(s.imago/counter).exists())
    abrupt_hub_stop(s)
    try:
        local=wait_for(lambda:(lambda r:r if r and r['status']=='done' else None)(agent_record(s,receipt['operation_id'])),timeout=10)
        assert local['acked']==0 and json.loads(local['result'])['ok']
    finally: restart_hub(s)
    op=s.poll(receipt['operation_id'],20)
    assert op['state']=='succeeded' and '测试日志完整 / DONE' in op['output']
    for _ in range(4): assert s.call('tasks_run',args)['operation_id']==receipt['operation_id']
    assert (s.imago/counter).read_text()=='x'
    wait_for(lambda:agent_record(s,receipt['operation_id'])['acked']==1)


def test_concurrent_lost_response_retries_write_one_backup(stack):
    s=stack;name='concurrent-'+key()+'.txt';args={'project':'Imago','path':name,'expected_sha256':'new','content':'one intentional write','idempotency_key':key()}
    def call(_):
        response=s.mcp('fs_write',args);assert not response['isError'];return response['structuredContent']['operation_id']
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool: ids=list(pool.map(call,range(12)))
    assert len(set(ids))==1 and s.poll(ids[0])['state']=='succeeded'
    assert (s.imago/name).read_text()=='one intentional write'
    assert len(s.fs('history_list',path=name)['backups'])==1


def test_offline_cancel_persists_across_hub_restart(stack):
    s=stack;s.stop_agent();wait_for(lambda:not online(s))
    try:
        r=s.call('tasks_run',{'project':'Imago','task':'smoke','idempotency_key':key()})
        cancelled=s.call('operations_cancel',{'operation_id':r['operation_id']})
        assert cancelled['state']=='cancelled'
        abrupt_hub_stop(s);s.start_hub()
    finally: s.start_agent()
    assert s.poll(r['operation_id'])['state']=='cancelled'
    assert agent_record(s,r['operation_id']) is None


def test_pending_grant_revocation_prevents_delayed_write(stack):
    s=stack
    grant=s.must(s.client.post('/api/grants',json={'label':'temporary-queued','scopes':['read','write'],'projects':[s.project['id']],'days':1}))
    name='revoked-'+key()+'.txt'
    s.stop_agent();wait_for(lambda:not online(s))
    try:
        receipt=s.mcp('fs_write',{'project':'Imago','path':name,'content':'must not write','expected_sha256':'new','idempotency_key':key()},grant['token'])['structuredContent']
        s.must(s.client.delete('/api/grants/'+grant['grant_id']))
    finally:s.start_agent()
    op=s.poll(receipt['operation_id'],15)
    assert op['state']=='failed' and op['result']['error']['code']=='AUTHORIZATION_CHANGED'
    assert not (s.imago/name).exists()


def test_running_task_can_be_cancelled_while_device_offline(stack):
    s=stack;counter='cancel-counter-'+key()+'.txt';name='cancel-'+key()[:8]
    code=f'import pathlib,time; pathlib.Path({counter!r}).write_text("started"); print("STARTED",flush=True); time.sleep(18); pathlib.Path({(counter+".finished")!r}).write_text("done")'
    install_task(s,name,[sys.executable,'-u','-c',code],timeout=25)
    receipt=s.call('tasks_run',{'project':'Imago','task':name,'idempotency_key':key()})
    wait_for(lambda:(s.imago/counter).exists())
    s.must(s.client.patch('/api/devices/'+s.device,json={'enabled':False}));wait_for(lambda:not online(s))
    try:
        cancel=s.call('operations_cancel',{'operation_id':receipt['operation_id']})
        assert cancel['cancel_requested']
        assert s.client.get('/api/operations/'+receipt['operation_id']).json()['pending']
    finally:s.must(s.client.patch('/api/devices/'+s.device,json={'enabled':True}))
    op=s.poll(receipt['operation_id'],20)
    assert op['state']=='cancelled' and not (s.imago/(counter+'.finished')).exists()


def test_actual_pytest_failure_is_not_retried_as_network_failure(stack):
    s=stack;testfile=s.imago/'tests'/'test_expected_failure.py';name='pytest-failure-'+key()[:8]
    testfile.write_text('def test_expected_failure():\n    assert False, "intentional regression fixture"\n')
    install_task(s,name,[sys.executable,'-m','pytest','-q','tests/test_expected_failure.py'])
    try:
        k=key();args={'project':'Imago','task':name,'idempotency_key':k};receipt=s.call('tasks_run',args)
        op=s.poll(receipt['operation_id'],15)
        assert op['state']=='failed' and op['result']['data']['exit_code']==1 and '1 failed' in op['output']
        assert s.call('tasks_run',args)['operation_id']==receipt['operation_id']
        assert len(s.call('operations_list',{'idempotency_key':k})['operations'])==1
    finally:testfile.unlink()


def test_malformed_hot_config_preserves_connection_and_recovers(stack):
    s=stack;original=copy.deepcopy(s.config)
    s.config_path.write_text('{"hub_url": null}')
    time.sleep(2.2)
    assert s.agent.poll() is None and s.fs('fs_read',path='README.md')['content'].startswith('# Imago')
    atomic_json(s.config_path,original);time.sleep(2.2)
    assert s.agent.poll() is None
    wait_for(lambda:online(s),timeout=15)
    assert s.fs('fs_read',path='README.md')['content'].startswith('# Imago')


def test_multiple_hub_restarts_keep_completed_write_receipt(stack):
    s=stack;name='restart-write-'+key()+'.txt';args={'project':'Imago','path':name,'content':'persistent write','expected_sha256':'new','idempotency_key':key()}
    first=s.fs('fs_write',**{k:v for k,v in args.items() if k!='project'})
    for _ in range(3):
        abrupt_hub_stop(s);restart_hub(s)
        assert s.call('fs_write',args)['operation_id']==first['operation_id']
    assert (s.imago/name).read_text()=='persistent write'
    assert len(s.fs('history_list',path=name)['backups'])==1


def test_operations_list_cannot_cross_token_scope(stack):
    s=stack;receipt=s.mcp('fs_read',{'project':'Imago','path':'README.md','idempotency_key':key()})['structuredContent']
    grant=s.must(s.client.post('/api/grants',json={'label':'different-grant','scopes':['read'],'projects':[s.project['id']],'days':1}))
    result=s.mcp('operations_list',{'project':'Imago'},grant['token'])['structuredContent']
    assert receipt['operation_id'] not in [x['id'] for x in result['operations']]
    assert s.mcp('operations_wait',{'operation_id':receipt['operation_id'],'wait_seconds':0},grant['token'])['isError']
