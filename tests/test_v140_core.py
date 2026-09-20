import asyncio
import base64
import dataclasses
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest
from agent.artifacts import CHUNK
from agent.searches import Searches
from agent.symbols import analyze
from hub.artifacts import byte_range
from shared.build_info import BuildIdentity,source_identity
from shared.contracts import TOOLS
from shared.util import DevError, VERSION
from tests.test_audit_agent import local_agent
from tests.test_agentdock_workflows import env,operation,call


def args(name,**values):
    return TOOLS[name].model.model_validate({'project':'fixture',**values}).model_dump()


def project(root):
    return {'id':'p','alias':'fixture','root':str(root),'mode':'write','allow_tasks':True}


def test_build_identity_separates_loaded_code_from_disk(tmp_path,monkeypatch):
    import shared.build_info as module
    (tmp_path/'shared').mkdir();(tmp_path/'agent').mkdir()
    version=tmp_path/'shared/util.py';version.write_text(f'VERSION = "{VERSION}"\n')
    code=tmp_path/'agent/main.py';code.write_text('value = 1\n')
    monkeypatch.setattr(module,'source_identity',lambda component:source_identity(component,tmp_path))
    build=BuildIdentity('agent');first=build.describe()
    assert not first['restart_required']
    code.write_text('value = 2\n')
    changed=build.describe()
    assert changed['restart_required'] and changed['runtime']==first['runtime']
    assert changed['disk']['source_sha256']!=first['disk']['source_sha256']


@pytest.mark.parametrize('range_header,size,expected',[(None,0,(0,-1,False)),(None,10,(0,9,False)),('bytes=2-4',10,(2,4,True)),('bytes=-4',10,(6,9,True)),('bytes=5-',10,(5,9,True)),('bytes=0-999',10,(0,9,True))])
def test_ranges(range_header,size,expected):assert byte_range(range_header,size)==expected


@pytest.mark.parametrize('value,size',[('bytes=-0',10),('bytes=10-',10),('bytes=4-2',10),('bytes=0-1,3-4',10),('bytes=0-0',0),('items=0-1',10),('bytes=-',10)])
def test_bad_ranges(value,size):
    with pytest.raises(DevError) as exc:byte_range(value,size)
    assert exc.value.status==416


def test_artifact_snapshot_is_immutable_chunked_and_integrity_checked(local_agent):
    agent,root=local_agent;content=bytes(range(256))*(CHUNK//256*2+7)
    source=root/'archive.zip';source.write_bytes(content)
    identifier=uuid.uuid4().hex;request=args('artifacts_register',path='archive.zip',idempotency_key=uuid.uuid4().hex)
    artifact=agent.artifacts.register(identifier,project(root),request)
    assert artifact['bytes']==len(content) and artifact['sha256']==hashlib.sha256(content).hexdigest()
    source.write_bytes(b'changed original')
    retrieved=b''.join(base64.b64decode(agent.artifacts.chunk(project(root),identifier,i)['data']) for i in range(0,len(content),CHUNK))
    assert retrieved==content
    assert agent.artifacts.register(identifier,project(root),request)==artifact
    path=agent.artifacts.directory/(identifier+'.bin')
    with path.open('r+b') as stream:stream.write(b'corrupted!')
    with pytest.raises(DevError) as exc:agent.artifacts.chunk(project(root),identifier,0)
    assert exc.value.code=='ARTIFACT_CORRUPT'


def test_artifact_short_reads_keep_fixed_chunk_boundaries(local_agent,monkeypatch):
    import agent.artifacts as module
    agent,root=local_agent;content=b'x'*(CHUNK+22);(root/'out.bin').write_bytes(content)
    original=module.os.read
    monkeypatch.setattr(module.os,'read',lambda fd,n:original(fd,min(n,10001)))
    identifier=uuid.uuid4().hex
    agent.artifacts.register(identifier,project(root),args('artifacts_register',path='out.bin',idempotency_key=uuid.uuid4().hex))
    assert len(base64.b64decode(agent.artifacts.chunk(project(root),identifier,0)['data']))==CHUNK
    assert len(base64.b64decode(agent.artifacts.chunk(project(root),identifier,CHUNK)['data']))==22


@pytest.mark.parametrize('kind',['secret','symlink','hardlink','directory','readonly','name'])
def test_artifact_respects_existing_file_boundaries(local_agent,kind,tmp_path):
    agent,root=local_agent;source=root/'out.zip';source.write_bytes(b'content')
    request=args('artifacts_register',path='out.zip',idempotency_key=uuid.uuid4().hex);p=project(root)
    if kind=='secret':request['path']='.env';(root/'.env').write_text('private')
    if kind=='symlink':source.unlink();source.symlink_to(tmp_path/'outside')
    if kind=='hardlink':os.link(source,root/'second')
    if kind=='directory':request['path']='.'
    if kind=='readonly':p['mode']='read'
    if kind=='name':request['name']='bad\nname'
    with pytest.raises((DevError,OSError)):agent.artifacts.register(uuid.uuid4().hex,p,request)


@pytest.mark.parametrize('path,source,names',[
    ('x.py','class C:\n async def run(self):\n  def inner(): return work()\n  return inner()\n',['C','C.run','C.run.inner']),
    ('x.js','export function go(){return true} class C { run(){return go()} }',['go','C','C.run']),
    ('x.ts','interface Person { name: string }; const go = (x: number) => x; class C { run(){return go(1)} }',['Person','go','C','C.run']),
    ('x.tsx','export const App = () => <div>Hello</div>;',['App'])])
def test_symbols_use_real_parsers(path,source,names):
    result=analyze(path,source.encode())
    assert [s['qualified_name'] for s in result['symbols']]==names
    assert all(s['end_line']>=s['line'] for s in result['symbols'])
    assert 'no cross-file type resolution' in result['precision']


@pytest.mark.parametrize('path,source',[('x.py','def broken('),('x.ts','function {'),('x.txt','hello')])
def test_symbols_do_not_fabricate_unparsed_results(path,source):
    with pytest.raises(DevError):analyze(path,source.encode())


def test_search_pages_survive_restart_without_rescanning_and_mark_stale(local_agent):
    agent,root=local_agent
    for i in range(5):(root/f'{i}.py').write_text('def needle():\n return 1\n')
    identifier=uuid.uuid4().hex;p=project(root)
    agent.searches.start(identifier,p,args('searches_start',query='needle',mode='symbols'))
    page=agent.searches.get(p,args('searches_get',search_id=identifier,limit=2))
    assert len(page['results'])==2 and page['has_more'] and not any(x['stale'] for x in page['results'])
    captured=page['results'][0];(root/captured['path']).write_text('changed')
    restored=Searches(agent.engine)
    again=restored.get(p,args('searches_get',search_id=identifier,limit=2))
    assert again['results'][0]['stale'] and again['results'][0]['sha256']==captured['sha256']
    next_page=restored.get(p,args('searches_get',search_id=identifier,cursor=page['cursor'],limit=3))
    assert len(next_page['results'])==3 and not next_page['has_more']
    assert not set(x['seq'] for x in page['results'])&set(x['seq'] for x in next_page['results'])


def test_search_cancel_budget_and_scoped_root(local_agent,tmp_path):
    agent,root=local_agent;(root/'hit.txt').write_text('needle\n'*20);p=project(root)
    identifier=uuid.uuid4().hex
    agent.searches.start(identifier,p,args('searches_start',query='needle',max_results=3))
    result=agent.searches.get(p,args('searches_get',search_id=identifier))
    assert result['result_count']==3 and result['truncated']
    identifier=uuid.uuid4().hex
    agent.searches.start(identifier,p,args('searches_start',query='needle'))
    assert agent.searches.cancel(p,{'search_id':identifier})['state']=='cancelled'
    assert agent.searches.get(p,args('searches_get',search_id=identifier))['scanned_files']==0
    other=dict(p,id='other')
    with pytest.raises(DevError):agent.searches.get(other,args('searches_get',search_id=identifier))


def test_diagnostics_filter_devices_and_blocking_operation_ids(env):
    runtime,owner=env
    visible=operation(env,finish=False);hidden=operation(env,grant='another',finish=False)
    runtime.diagnostics.ingest(visible,[{'seq':1,'stage':'waiting_project','elapsed_ms':100,'detail':{'blocked_by':[hidden]}}])
    trace=call(env,'operations_trace',operation_id=visible)
    assert trace['events'][0]['blocked_by']==[]
    unknown=call(env,'diagnostics_get')
    assert unknown['client_catalog']['matches'] is None and len(unknown['devices'])==1
    match=call(env,'diagnostics_get',client_catalog_sha256=unknown['hub']['catalog_sha256'])
    assert match['client_catalog']['matches'] is True
    no_access=(runtime,dataclasses.replace(owner,projects=[]))
    assert call(no_access,'diagnostics_get')['devices']==[]
    runtime.complete({'id':visible},{'ok':True,'data':{'exit_code':0},'trace':[{'seq':2,'stage':'executing','elapsed_ms':101,'detail':{'blocked_by':[hidden]}}]})
    assert hidden not in json.dumps(call(env,'operations_get',operation_id=visible))


def test_trace_duplicate_events_and_untrusted_stage_input(env):
    runtime,owner=env;identifier=operation(env,finish=False)
    event={'seq':1,'stage':'executing','elapsed_ms':10,'detail':{}}
    runtime.diagnostics.ingest(identifier,[event,event,{'stage':[], 'seq':2},{'stage':'executing','seq':True,'elapsed_ms':2}])
    trace=call(env,'operations_trace',operation_id=identifier)
    assert len(trace['events'])==1


@pytest.mark.asyncio
async def test_artifact_channel_fences_old_connections_and_cleans_cancelled_requests(env,monkeypatch):
    runtime,_=env
    class Peer:
        async def send(self,data):self.last=data
    peer=Peer();runtime.connections['d']=peer;monkeypatch.setattr(runtime,'online',lambda _:True)
    data=b'fixture';sha=hashlib.sha256(data).hexdigest()
    row={'id':uuid.uuid4().hex,'device_id':'d','bytes':len(data),'sha256':sha}
    p={'id':'p','alias':'P','root':'/tmp/project','mode':'write','allow_tasks':True}
    task=asyncio.create_task(runtime.artifacts.chunk(row,p,0));await asyncio.sleep(0)
    response={'request_id':peer.last['request_id'],'result':{'ok':True,'data':{'artifact_id':row['id'],'offset':0,'sha256':sha,'chunk_sha256':sha,'data':base64.b64encode(data).decode()}}}
    runtime.artifacts.receive(Peer(),response);await asyncio.sleep(0);assert not task.done()
    runtime.artifacts.receive(peer,response);assert await task==data
    assert not runtime.artifacts.pending
    task=asyncio.create_task(runtime.artifacts.chunk(row,p,0));await asyncio.sleep(0);task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    assert not runtime.artifacts.pending


def test_search_manifest_has_explicit_quota_and_expiration(local_agent,monkeypatch):
    import agent.searches as module
    agent,root=local_agent
    for i in range(20):(root/f'file{i}.txt').write_text('needle')
    monkeypatch.setattr(module,'MAX_STORAGE',500)
    identifier=uuid.uuid4().hex;p=project(root)
    started=agent.searches.start(identifier,p,args('searches_start',query='needle'))
    assert started['truncated'] and started['file_count']<20
    with agent.journal.lock,agent.journal.db:
        agent.journal.db.execute('UPDATE search_sessions SET expires=1 WHERE id=?',(identifier,))
    with pytest.raises(DevError) as exc:agent.searches.get(p,args('searches_get',search_id=identifier))
    assert exc.value.code=='SEARCH_EXPIRED'


def test_symbol_metadata_budget_is_controlled(monkeypatch):
    import agent.symbols as module
    monkeypatch.setattr(module,'MAX_METADATA_BYTES',100)
    with pytest.raises(DevError) as exc:analyze('x.py',b'def function_with_name():\n return other_name\n')
    assert exc.value.code=='CODE_ANALYSIS_LIMIT'


def test_search_rejects_cursor_that_skips_unsaved_results(local_agent):
    agent,root=local_agent;(root/'file.py').write_text('needle');identifier=uuid.uuid4().hex;p=project(root)
    agent.searches.start(identifier,p,args('searches_start',query='needle'))
    with pytest.raises(DevError) as exc:agent.searches.get(p,args('searches_get',search_id=identifier,cursor=999))
    assert exc.value.code=='INVALID_SEARCH_CURSOR'


def test_new_tool_reports_agent_upgrade_before_dispatch(env,monkeypatch):
    runtime,_=env
    runtime.store.execute("UPDATE devices SET info=? WHERE id='d'",(json.dumps({'capabilities':['fs_read']}),))
    monkeypatch.setattr(runtime,'online',lambda _:True)
    with pytest.raises(DevError) as exc:call(env,'code_symbols',project='P',path='x.py')
    assert exc.value.code=='AGENT_UPGRADE_REQUIRED'
    assert runtime.store.one('SELECT COUNT(*) n FROM operations')['n']==0


def test_fixture_health_uses_direct_client_and_validates_response(monkeypatch):
    from types import SimpleNamespace
    import httpx
    import tests.support as support
    real_client=httpx.Client;observed=[];calls=[]
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200,json={'status':'ok'}) if len(calls)>1 else httpx.Response(200,text='<html>not the hub</html>')
    def factory(**kwargs):
        observed.append(kwargs)
        return real_client(**kwargs,transport=httpx.MockTransport(handler))
    monkeypatch.setattr(support.httpx,'Client',factory)
    support.wait_for_hub(SimpleNamespace(poll=lambda:None),'http://127.0.0.1:12345')
    assert observed[0]['trust_env'] is False and len(calls)==2


def test_fixture_health_reports_child_exit_without_waiting(monkeypatch):
    from types import SimpleNamespace
    from tests.support import wait_for_hub
    with pytest.raises(AssertionError,match='exited with code 7'):
        wait_for_hub(SimpleNamespace(poll=lambda:7),'http://127.0.0.1:12345')
