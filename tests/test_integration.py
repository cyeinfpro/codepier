import base64, hashlib, json, os, time, uuid
from urllib.parse import urlparse, parse_qs
import httpx, pytest
from shared.crypto import digest
from shared.util import atomic_json
from tests.support import wait_for
from tests.catalog_assertions import assert_task_catalog

def ident():return uuid.uuid4().hex

def test_real_outbound_agent_online_and_alias(stack):
    devices=stack.client.get('/api/devices').json()['devices'];assert devices[0]['online']
    assert stack.call('projects_resolve',{'project':'iMaGo'})['root']==str(stack.imago)
    assert len(stack.call('projects_list')['projects'])==3


def test_source_checkout_agent_reports_safe_repair_state(stack):
    device=stack.client.get('/api/devices').json()['devices'][0]
    assert device['online'] and not device['agent']['managed']
    assert device['agent']['actions']==[] and not device['agent']['can_update']
    assert '一键安装器管理' in device['agent']['reason']
    response=stack.client.post(f"/api/devices/{stack.device}/agent-update",json={
        'idempotency_key':'integration-lifecycle-001','confirmation':''})
    assert response.status_code==409
    assert response.json()['error']['code']=='AGENT_UPGRADE_REQUIRED'

def test_panel_auth_csrf_and_origin(stack):
    assert httpx.get(stack.url+'/api/overview').status_code==401
    assert httpx.post(stack.url+'/api/login',json={'username':'admin','password':stack.password},headers={'Origin':'http://evil.invalid'}).status_code==403
    assert stack.client.post('/api/devices',json={'name':'no csrf','hub_url':stack.url},headers={'X-RD-CSRF':''}).status_code==403
    assert stack.client.post('/api/devices',json={'name':'cross origin','hub_url':stack.url},headers={'Origin':'http://evil.invalid'}).status_code==403

def test_read_tree_and_search_codepier(stack):
    r=stack.fs('fs_read',path='src/main.py',max_lines=2);assert r['truncated'] and r['sha256']
    t=stack.fs('fs_tree',depth=3,limit=100);assert 'src/main.py' in [x['path'] for x in t['entries']]
    assert '.env' not in [x['path'] for x in t['entries']]
    s=stack.fs('fs_search',query='greeting');assert s['matches']
    many=stack.fs('fs_read_many',paths=['README.md','missing.txt']);assert many['files'][0]['ok'] and not many['files'][1]['ok']

def test_mapping_validation_alias_uniqueness(stack):
    body={'alias':'IMAGO','device_id':stack.device,'root':str(stack.imago),'mode':'write','allow_tasks':False}
    assert stack.client.post('/api/projects',json=body).status_code==409
    body['alias']='Outside';body['root']=str(stack.directory)
    assert stack.client.post('/api/projects',json=body).status_code==409
    body['root']=str(stack.imago/'.git')
    assert stack.client.post('/api/projects',json=body).status_code==409

@pytest.mark.parametrize('path',['../outside.txt','.env','.git/config','/etc/passwd'])
def test_denied_files_not_returned(stack,path):
    r=stack.call('fs_read',{'project':'Imago','path':path},raw=True)
    assert r.status_code in (400,403,409);assert 'never-return-this' not in r.text

def test_write_exactly_once_and_conflict(stack):
    name='test-'+ident()+'.txt'; args={'path':name,'content':'one\n','expected_sha256':'new','idempotency_key':ident()}
    first=stack.fs('fs_write',**args);second=stack.fs('fs_write',**args)
    assert first['operation_id']==second['operation_id'];assert (stack.imago/name).read_text()=='one\n'
    assert stack.call('fs_write',{'project':'Imago',**args,'content':'other'},raw=True).status_code==409
    assert stack.call('fs_write',{'project':'Imago',**args,'idempotency_key':ident()},raw=True).status_code==409
    preview=stack.fs('fs_preview',path=name,content='two\n');assert preview['added_lines']==1
    edit=stack.fs('fs_edit',path=name,expected_sha256=first['sha256'],edits=[{'old_text':'one','new_text':'two'}],idempotency_key=ident())
    assert (stack.imago/name).read_text()=='two\n'
    restore=stack.fs('history_restore',backup_id=edit['backup_id'],expected_sha256=edit['sha256'],idempotency_key=ident())
    assert (stack.imago/name).read_text()=='one\n'
    stack.fs('fs_delete',path=name,expected_sha256=restore['sha256'],idempotency_key=ident());assert not (stack.imago/name).exists()

def test_external_modification_cas(stack):
    path='cas-'+ident()+'.txt';(stack.imago/path).write_text('old')
    old=stack.fs('fs_read',path=path);(stack.imago/path).write_text('changed locally')
    r=stack.call('fs_write',{'project':'Imago','path':path,'expected_sha256':old['sha256'],'content':'lost update','idempotency_key':ident()},raw=True)
    assert r.status_code==409 and (stack.imago/path).read_text()=='changed locally'

def test_mkdir_move_checkpoint_history(stack):
    folder='test-'+ident();stack.fs('fs_mkdir',path=folder+'/nested',idempotency_key=ident())
    f=stack.fs('fs_write',path=folder+'/nested/a.txt',content='abc',expected_sha256='new',idempotency_key=ident())
    stack.fs('fs_move',path=folder+'/nested/a.txt',destination=folder+'/b.txt',expected_sha256=f['sha256'],idempotency_key=ident())
    assert (stack.imago/folder/'b.txt').exists()
    c=stack.fs('project_checkpoint',label='Integration checkpoint',idempotency_key=ident());assert c['files']>0
    assert stack.fs('history_list')['backups']

def test_tasks_success_failure_timeout_cancel(stack):
    ok=stack.fs('tasks_run',task='smoke',idempotency_key=ident());out=stack.poll(ok['operation_id']);assert out['state']=='succeeded' and 'PASS' in out['output']
    bad=stack.fs('tasks_run',task='failure',idempotency_key=ident());assert stack.poll(bad['operation_id'])['state']=='failed'
    timed=stack.fs('tasks_run',task='timeout',idempotency_key=ident());t=stack.poll(timed['operation_id']);assert t['state']=='failed' and t['result']['data']['timed_out']
    slow=stack.fs('tasks_run',task='slow',idempotency_key=ident())
    wait_for(lambda:'STARTED' in stack.client.get('/api/operations/'+slow['operation_id']).json()['output'])
    stack.call('operations_cancel',{'operation_id':slow['operation_id']})
    assert stack.poll(slow['operation_id'])['state']=='cancelled'
    assert stack.call('tasks_run',{'project':'Imago','task':'arbitrary-command','idempotency_key':ident()},raw=True).json()['pending']
    # Asynchronous failures are recorded by the Agent, not hidden in a submit response.

def test_mcp_protocol_and_catalog(stack):
    r=stack.rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'integration','version':'1'}})
    assert r.json()['result']['protocolVersion']=='2025-11-25'
    catalog=stack.rpc('tools/list').json()['result']['tools']
    assert_task_catalog(catalog, 9)
    assert not {'integration_control','validations_accept'} & {t['name'] for t in catalog}
    assert stack.rpc('ping').json()['result']=={}
    resources=stack.rpc('resources/list').json()['result']['resources']
    assert {r['uri'] for r in resources}=={'rd://projects','rd://workflow','ui://codepier/workspace-v1.html','ui://codepier/changes-v1.html'}
    assert 'Imago' in stack.rpc('resources/read',{'uri':'rd://projects'}).text
    assert stack.rpc('prompts/get',{'name':'review_project','arguments':{'project':'Imago'}}).json()['result']['messages']
    assert stack.mcp('read',{'project':'Imago','path':'README.md'})['structuredContent']['content'].startswith('# Imago')
    assert stack.client.get('/mcp').status_code==405
    assert stack.client.delete('/mcp').status_code==405
    assert stack.rpc('ping',headers={'Accept':'application/json'}).status_code==406
    assert stack.rpc('ping',headers={'Origin':'http://evil.invalid'}).status_code==403
    assert stack.rpc('ping',headers={'MCP-Protocol-Version':'not-a-version'}).status_code==400
    unauth=httpx.post(stack.url+'/mcp',json={});assert unauth.status_code==401 and 'resource_metadata' in unauth.headers['www-authenticate']
    headers={'Authorization':'Bearer '+stack.pat,'Accept':'application/json, text/event-stream'}
    assert stack.client.post('/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'},headers=headers).status_code==202

def test_scoped_token_and_revocation(stack):
    token=stack.must(stack.client.post('/api/grants',json={'label':'read Imago only','scopes':['read'],'projects':[stack.project['id']],'days':1}))
    read=stack.mcp('workspace', {'operation': 'list'},token_value=token['token']);assert len(read['structuredContent']['projects'])==1
    no_project=stack.mcp('read',{'project':'Nexus','path':'README.md'},token['token']);assert no_project['isError']
    no_write=stack.mcp('write',{'project':'Imago','path':'blocked.txt','content':'bad','expected_sha256':'new','idempotency_key':ident()},token['token']);assert no_write['isError']
    stack.must(stack.client.delete('/api/grants/'+token['grant_id']))
    assert stack.rpc('ping',token_value=token['token']).status_code==401

def oauth_prepare(stack,scope='read write execute'):
    reg=stack.must(stack.client.post('/oauth/register',json={'client_name':'Integration OAuth','redirect_uris':['http://localhost:19123/callback'],'token_endpoint_auth_method':'none'}))
    verifier='v'*64;challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    args={'response_type':'code','client_id':reg['client_id'],'redirect_uri':'http://localhost:19123/callback','scope':scope,'state':'test-state','resource':stack.url+'/mcp','code_challenge_method':'S256','code_challenge':challenge}
    auth=stack.client.get('/oauth/authorize',params=args);assert auth.status_code==303
    id=parse_qs(urlparse(auth.headers['location']).query)['authorize'][0]
    return reg,id,verifier,args

def test_oauth_pkce_full_flow_refresh_and_replay(stack):
    reg,id,verifier,args=oauth_prepare(stack)
    details=stack.client.get('/api/oauth/requests/'+id);assert details.json()['client_name']=='Integration OAuth'
    assert stack.client.post('/api/oauth/requests/'+id+'/decide',json={'allow':True,'scopes':['read'],'projects':[]}).status_code==400
    decision=stack.must(stack.client.post('/api/oauth/requests/'+id+'/decide',json={'allow':True,'scopes':['read','write'],'projects':[stack.project['id']]}))
    code=parse_qs(urlparse(decision['redirect']).query)['code'][0]
    form={'grant_type':'authorization_code','code':code,'client_id':reg['client_id'],'redirect_uri':args['redirect_uri'],'code_verifier':verifier,'resource':stack.url+'/mcp'}
    assert stack.client.post('/oauth/token',data={**form,'code_verifier':'x'*64}).status_code==400
    tokens=stack.must(stack.client.post('/oauth/token',data=form));assert 'access_token' in tokens
    assert not stack.mcp('workspace', {'operation': 'list'},token_value=tokens['access_token'])['isError']
    assert stack.client.post('/oauth/token',data=form).status_code==400
    refresh={'grant_type':'refresh_token','client_id':reg['client_id'],'refresh_token':tokens['refresh_token'],'resource':stack.url+'/mcp'}
    new=stack.must(stack.client.post('/oauth/token',data=refresh));assert new['refresh_token']!=tokens['refresh_token']
    assert stack.client.post('/oauth/token',data=refresh).status_code==400
    assert stack.client.post('/api/oauth/requests/'+id+'/decide',json={'allow':True,'scopes':['read'],'projects':[stack.project['id']]}).status_code==410
    assert stack.client.post('/oauth/revoke',data={'token':new['access_token'],'client_id':reg['client_id']}).status_code==200
    assert stack.rpc('ping',token_value=new['access_token']).status_code==401

@pytest.mark.parametrize('uri',['http://public.example/callback','https://chatgpt.com.evil.invalid/callback','https://user:secret@chatgpt.com/callback','https://chatgpt.com/callback#frag'])
def test_oauth_reject_redirects(stack,uri):
    assert stack.client.post('/oauth/register',json={'redirect_uris':[uri]}).status_code==400

@pytest.mark.parametrize('body',[[],None,42,{'redirect_uris':[]},{'redirect_uris':['http://localhost/x'],'token_endpoint_auth_method':'client_secret_basic'}])
def test_oauth_malformed_register(stack,body):
    r=stack.client.post('/oauth/register',content=json.dumps(body),headers={'Content-Type':'application/json'})
    assert r.status_code in (400,429)

def test_audit_tracks_real_mcp_and_diff(stack):
    m=stack.mcp('write',{'project':'Imago','path':'audit-example.txt','content':'Audited fixture\n','expected_sha256':'new','idempotency_key':ident()})
    id=m['structuredContent']['operation_id'];r=stack.client.get('/api/operations/'+id).json()
    assert r['actor'].startswith('mcp:') and r['result']['data']['diff']
    assert 'Audited fixture' not in json.dumps(r['args_summary'])
    csv=stack.client.get('/api/audit-export');assert csv.status_code==200 and 'fs_write' in csv.text
    assert stack.pat not in csv.text and stack.pairing['secret'] not in csv.text

def test_offline_queues_then_reconnects(stack):
    stack.stop_agent();wait_for(lambda:not stack.client.get('/api/devices').json()['devices'][0]['online'])
    result=stack.call('fs_write',{'project':'Imago','path':'offline.txt','expected_sha256':'new','content':'no','idempotency_key':ident()},raw=True)
    assert result.status_code==200 and result.json()['pending'] and not (stack.imago/'offline.txt').exists()
    stack.start_agent()
    assert stack.poll(result.json()['operation_id'])['state']=='succeeded'
    assert (stack.imago/'offline.txt').read_text()=='no'
    assert stack.fs('fs_read',path='README.md')['content']

def test_hot_reload_reconnect(stack):
    old=stack.client.get('/api/devices').json()['devices'][0]['last_seen']
    c=dict(stack.config);c['name']='Reloaded device';atomic_json(stack.config_path,c)
    wait_for(lambda:stack.client.get('/api/devices').json()['devices'][0]['info'].get('name')=='Reloaded device',timeout=12)
    assert stack.fs('fs_read',path='README.md')['content']

def test_hub_restart_preserves_completed_task_and_reconnects(stack):
    task=stack.fs('tasks_run',task='smoke',idempotency_key=ident());id=task['operation_id']
    result=stack.poll(id);assert result['state']=='succeeded'
    stack.hub.terminate();stack.hub.wait(timeout=12);stack.start_hub()
    wait_for(lambda:stack.client.get('/api/devices').json()['devices'][0]['online'],timeout=16)
    assert stack.client.get('/api/operations/'+id).json()['state']=='succeeded'
    assert stack.fs('fs_read',path='README.md')['content']


def test_result_outbox_survives_disconnection_without_reexecution(stack):
    import sqlite3
    counter=stack.imago/'outbox-count.txt'
    config=json.loads(stack.config_path.read_text())
    config['tasks']['delayed']={'command':[__import__('sys').executable,'-u','-c',
        'from pathlib import Path; import time; p=Path("outbox-count.txt"); p.write_text(p.read_text()+"x" if p.exists() else "x"); print("STARTED", flush=True); time.sleep(2); print("OUTBOX_DONE")'],
        'projects':['Imago'],'timeout':10}
    atomic_json(stack.config_path,config)
    wait_for(lambda:any(t['name']=='delayed' for t in stack.fs('tasks_list')['tasks']),timeout=12)
    job=stack.fs('tasks_run',task='delayed',idempotency_key=ident());opid=job['operation_id']
    wait_for(lambda:counter.exists())
    stack.must(stack.client.patch('/api/devices/'+stack.device,json={'enabled':False}))
    def local_done():
        with sqlite3.connect(stack.directory/'agent-state'/'agent.sqlite3') as db:
            row=db.execute('SELECT status,acked FROM calls WHERE id=?',(opid,)).fetchone()
            return row and row[0]=='done' and row[1]==0
    wait_for(local_done,timeout=10)
    stack.must(stack.client.patch('/api/devices/'+stack.device,json={'enabled':True}))
    end=stack.poll(opid,timeout=25)
    assert end['state']=='succeeded' and 'OUTBOX_DONE' in end['result']['data']['output']
    assert counter.read_text()=='x'
    assert stack.client.get('/api/operations/'+opid).json()['id']==opid

def test_panel_sse_ready_and_real_operation_notification(stack):
    with stack.client.stream('GET','/api/events',timeout=8) as response:
        assert response.status_code==200 and 'text/event-stream' in response.headers['content-type']
        lines=response.iter_lines();assert next(lines)=='event: ready'
        assert next(lines)=='data: {}'
        assert next(lines)==''
        read=stack.fs('fs_read',path='README.md')
        operation_id=read['operation_id']
        event=None;seen=[]
        for line in lines:
            if not line.startswith('data: '):continue
            candidate=json.loads(line[6:]);seen.append(candidate.get('type'))
            # Trace and unrelated notifications may arrive first. Require the
            # actual operation notification for this read, not just any event.
            if candidate.get('type')=='operation' and candidate.get('data',{}).get('id')==operation_id:
                event=candidate;break
            if len(seen)>=100:break
        assert event,{'operation_id':operation_id,'received_types':seen}

def test_hub_backup_preserves_database_and_master_key(stack):
    import sqlite3, subprocess, sys, zipfile
    from tests.support import BASE
    target=stack.directory/'snapshot.zip'
    result=subprocess.run([sys.executable,'-m','hub','--data-dir',str(stack.hubdir),'backup','--output',str(target)],cwd=BASE,capture_output=True,text=True,timeout=8)
    assert result.returncode==0,result.stderr
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist())=={'hub.sqlite3','master.key'}
        assert archive.read('master.key')==(stack.hubdir/'master.key').read_bytes()
        recovered=stack.directory/'restore-check';recovered.mkdir();archive.extractall(recovered)
    with sqlite3.connect(recovered/'hub.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM devices').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM projects').fetchone()[0]==3
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert target.stat().st_mode & 0o077==0
