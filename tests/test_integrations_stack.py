"""Real HTTP -> encrypted Hub/Agent -> Git, processes, LSP and durable state.

The LSP endpoint is a deterministic stdio fixture; no paid model is invoked.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import sys
import time
import uuid

import httpx
import pytest
from shared.util import atomic_json
from shared.contracts import OUTPUT_SCHEMAS
from shared.mcp_protocol import MODERN, PREFIX
from jsonschema import Draft202012Validator
from tests.support import running_stack, wait_for, BASE
from hub.core_tools import public_call


@pytest.fixture(scope='module')
def integrated_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('integrations-real-stack')) as s:
        s.stop_agent()
        s.config['shell']={'enabled':True,'projects':['*'],'command':['/bin/sh','-c']}
        s.config['mcp_policy']={'block_local_codex':True}
        s.config['integrations']={'local_control':True,'language_servers':{
            'python':{'command':[sys.executable,str(BASE/'tests/fake_lsp_server.py'),'--mode','cold','--marker',str(s.directory/'lsp-marker.json')],
                      'projects':['Imago'],'timeout_seconds':5}}}
        atomic_json(s.config_path,s.config);s.start_agent()
        (s.imago/'source.py').write_text('source = 1\n')
        (s.imago/'helper.py').write_text('helper = 2\n')
        yield s


def resolved(s,name,args=None,*,panel=False,expect='succeeded'):
    args={'project':'Imago',**(args or {})}
    if name not in {'activity_list','workflows_handoff'}:args.setdefault('idempotency_key',uuid.uuid4().hex)
    public_name, public_args = public_call(name, args)
    result=s.call(name,args) if panel else s.mcp(public_name,public_args)['structuredContent']
    if result.get('pending'):
        operation=s.poll(result['operation_id'],timeout=35)
        assert operation['state']==expect,operation
        result={**(operation.get('result') or {}).get('data',{}),'operation_id':operation['id']}
    assert not result.get('error'),result
    Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(result)
    return result


def modern(s,method,params=None,*,profile='full',**overrides):
    params=dict(params or {})
    params['_meta']={PREFIX+'protocolVersion':MODERN,PREFIX+'clientCapabilities':{},
                     PREFIX+'clientInfo':{'name':'CodePier independent wire test','version':'1'}}
    headers={'Authorization':'Bearer '+s.pat,'Content-Type':'application/json',
             'Accept':'application/json, text/event-stream','MCP-Protocol-Version':MODERN,'Mcp-Method':method}
    if method=='tools/call':headers['Mcp-Name']=params['name']
    if method=='resources/read':headers['Mcp-Name']=params['uri']
    headers.update(overrides)
    return s.client.post('/mcp?profile='+profile,headers=headers,json={'jsonrpc':'2.0','id':uuid.uuid4().hex,'method':method,'params':params})


def test_legacy_and_modern_protocols_are_distinct_and_share_permissions(integrated_stack):
    s=integrated_stack
    old=s.rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'old','version':'1'}})
    assert old.status_code==200 and old.json()['result']['protocolVersion']=='2025-11-25'
    new=modern(s,'server/discover')
    assert new.status_code==200,new.text
    result=new.json()['result'];assert result['resultType']=='complete' and MODERN in result['supportedVersions']
    tools=modern(s,'tools/list').json()['result']['tools']
    assert 'write' in {t['name'] for t in tools}
    assert not {'integration_control','validations_accept'}&{t['name'] for t in tools}
    read=modern(s,'tools/call',{'name':'read','arguments':{'project':'Imago','path':'README.md'}})
    assert read.status_code==200 and not read.json()['result']['isError'],read.text
    mismatch=modern(s,'tools/call',{'name':'read','arguments':{'project':'Imago','path':'README.md'}},**{'Mcp-Name':'edit'})
    assert mismatch.status_code>=400 and 'error' in mismatch.json()
    assert modern(s,'initialize').json().get('error')
    bad=modern(s,'tools/call',{'name':'integration_control','arguments':{'project':'Imago','action':'resume','confirm':'Imago','idempotency_key':uuid.uuid4().hex}})
    assert bad.json()['result']['isError']


def test_apps_resources_are_real_built_documents_and_bound_to_snapshots(integrated_stack):
    s=integrated_stack
    # New calls stay text-only; saved app instances can still resolve their resources.
    for profile in ('full', 'coding'):
        for definition in modern(s, 'tools/list', profile=profile).json()['result']['tools']:
            assert 'resourceUri' not in definition['_meta'].get('ui', {})
            assert 'openai/outputTemplate' not in definition['_meta']
    listing=s.rpc('resources/list').json()['result']['resources']
    assert {'ui://codepier/workspace-v1.html','ui://codepier/changes-v1.html'}<={r['uri'] for r in listing}
    card=s.rpc('resources/read',{'uri':'ui://codepier/changes-v1.html'}).json()['result']['contents'][0]
    assert card['mimeType']=='text/html;profile=mcp-app' and '<script>' in card['text']
    assert card['_meta']['ui']['csp']['connectDomains']==[]
    opened=s.mcp('workspace',{'project': 'Imago', 'capture_baseline': True, 'operation': 'open'})
    assert opened['_meta']['com.codepier/binding']['project']=='Imago'
    data=opened['structuredContent']
    if data.get('pending'):data=s.poll(data['operation_id'])['result']['data']
    (s.imago/'card-snapshot.txt').write_text('before later changes\n')
    review=s.mcp('read',{'project': 'Imago', 'operation': 'changes', 'options': {'baseline_ref': data['baseline_ref']}})
    value=review['structuredContent']
    if value.get('pending'):value=s.poll(value['operation_id'])['result']['data']
    first=resolved(s,'show_changes',{'review_ref':value['review_ref'],'path':'card-snapshot.txt'})
    (s.imago/'card-snapshot.txt').write_text('different current contents\n')
    assert resolved(s,'show_changes',{'review_ref':value['review_ref'],'path':'card-snapshot.txt'})['diff']==first['diff']


def test_worktree_is_real_isolated_scoped_and_safe_to_remove(integrated_stack):
    s=integrated_stack
    created=resolved(s,'worktrees_create',{'label':'isolated fixture'})
    workspace=created['workspace_id'];target=Path(created['path'])
    assert target.exists() and target!=s.imago and created['source_dirty']
    opened=resolved(s,'open_workspace',{'workspace_id':workspace})
    assert opened['workspace']['root']==str(target)
    result=resolved(s,'fs_write',{'workspace_id':workspace,'path':'isolated.txt','content':'only this worktree','expected_sha256':'new'})
    assert (target/'isolated.txt').read_text()=='only this worktree' and not (s.imago/'isolated.txt').exists()
    issued=s.client.post('/api/grants',json={'label':'other-worktree-owner','scopes':['read','write','execute'],'projects':[s.project['id']],'days':1}).json()
    denied=s.mcp('read',{'project':'Imago','workspace_id':workspace,'path':'README.md'},token_value=issued['token'])
    if denied['structuredContent'].get('pending'):
        assert s.poll(denied['structuredContent']['operation_id'])['state']=='failed'
    else:assert denied['isError']
    removal=s.mcp('workspace',{'project': 'Imago', 'idempotency_key': uuid.uuid4().hex, 'operation': 'worktree_remove', 'options': {'target_workspace_id': workspace, 'confirm': workspace}})['structuredContent']
    if removal.get('pending'):assert s.poll(removal['operation_id'])['state']=='failed'
    else:assert removal.get('error')
    assert target.exists()
    # Scoped searches and immutable artifact delivery also work from this workspace.
    search=resolved(s,'searches_start',{'workspace_id':workspace,'query':'only this','file_glob':'isolated.txt'})
    hits=resolved(s,'searches_get',{'workspace_id':workspace,'search_id':search['search_id']})
    assert len(hits['results'])==1
    archive=resolved(s,'artifacts_register',{'workspace_id':workspace,'path':'isolated.txt'})
    assert s.client.get(archive['download_path']).content==b'only this worktree'
    resolved(s,'apply_patch',{'workspace_id':workspace,'changes':[{'action':'delete','path':'isolated.txt','expected_sha256':result['sha256']}]})
    deleted=resolved(s,'worktrees_remove',{'target_workspace_id':workspace,'confirm':workspace})
    assert deleted['removed'] and not target.exists()
    assert s.client.get(archive['download_path']).content==b'only this worktree'
    s.client.delete('/api/grants/'+issued['grant_id'])


@pytest.mark.parametrize('action',['symbols','workspace_symbols','definition','references','hover','diagnostics','incoming_calls','outgoing_calls'])
def test_real_stdio_lsp_queries_and_owned_process_cleanup(integrated_stack,action):
    s=integrated_stack
    args={'action':action,'path':'source.py','language':'python','query':'fixture'}
    result=resolved(s,'lsp_query',args)
    assert result['backend']=='lsp' and result['precision']=='semantic' and result['column_unit']=='one-based Unicode scalar'
    if action=='hover':assert result['text']=='fixture: str'
    else:assert result['items']
    if action=='definition':assert result['omitted']>=1 and all(x['path']!='outside-private.py' for x in result['items'])
    if action=='references':assert len(result['items'])==2
    marker=json.loads((s.directory/'lsp-marker.json').read_text())
    assert marker['state']=='exited'
    with pytest.raises(ProcessLookupError):os.kill(marker['pid'],0)


def test_validation_is_current_then_stale_and_acceptance_is_panel_only(integrated_stack):
    s=integrated_stack
    key=uuid.uuid4().hex
    args={'command':'printf verified','label':'Controlled command','idempotency_key':key}
    first=resolved(s,'validation_run',args)
    again=resolved(s,'validation_run',args)
    assert again['operation_id']==first['operation_id'] and first['state']=='passed'
    vid=first['validation_id']
    denied=s.mcp('validations_accept',{'project':'Imago','validation_id':vid,'confirm':vid,'idempotency_key':uuid.uuid4().hex})
    assert denied['isError']
    accepted=resolved(s,'validations_accept',{'validation_id':vid,'confirm':vid,'decision':'accept','note':'Test owner reviewed'},panel=True)
    assert accepted['accepted_current']
    (s.imago/'after-validation.py').write_text('changed = True\n')
    stale=resolved(s,'validations_get',{'validation_id':vid})
    assert stale['state']=='stale' and stale['historical_state']=='passed' and not stale['accepted_current']
    failed=resolved(s,'validation_run',{'command':'exit 7'},expect='failed')
    assert failed['state']=='failed' and failed['exit_code']==7


def test_pause_keeps_reads_and_receipts_but_blocks_new_remote_writes(integrated_stack):
    s=integrated_stack
    previous=resolved(s,'fs_read',{'path':'README.md'})
    paused=resolved(s,'integration_control',{'action':'pause','confirm':'Imago'},panel=True)
    try:
        assert paused['paused']
        assert resolved(s,'fs_read',{'path':'README.md'})['content']==previous['content']
        denied=s.mcp('write',{'project':'Imago','path':'paused-write.txt','content':'no','expected_sha256':'new','idempotency_key':uuid.uuid4().hex})
        assert denied['isError'] and not (s.imago/'paused-write.txt').exists()
        assert not s.mcp('process',{'operation': 'get', 'operation_ids': [previous['operation_id']]})['isError']
        state=resolved(s,'readiness_get');assert state['admission']['paused']
    finally:
        assert not resolved(s,'integration_control',{'action':'resume','confirm':'Imago'},panel=True)['paused']


def test_emergency_stop_targets_owned_process_not_the_agent(integrated_stack):
    s=integrated_stack
    command=shlex.quote(sys.executable)+' -u -c '+shlex.quote('import time; print("STOP-FIXTURE-READY",flush=True); time.sleep(90)')
    receipt=s.mcp('exec',{'project': 'Imago', 'command': command, 'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0})['structuredContent']
    wait_for(lambda:'STOP-FIXTURE-READY' in s.client.get('/api/operations/'+receipt['operation_id']).json().get('output',''),timeout=15)
    try:
        stopped=resolved(s,'integration_control',{'action':'stop','confirm':'Imago','include_native':False},panel=True)
        assert receipt['operation_id'] in stopped['stopped_verified'],stopped
        assert s.poll(receipt['operation_id'])['state']=='cancelled'
        assert s.agent.poll() is None and resolved(s,'readiness_get')['admission']['paused']
    finally:resolved(s,'integration_control',{'action':'resume','confirm':'Imago'},panel=True)


def test_activity_is_real_timing_not_model_thinking_and_is_scoped(integrated_stack):
    s=integrated_stack
    for _ in range(2):
        result=s.rpc('tools/call',{'name':'read','arguments':{'project':'Imago','path':'README.md'},'_meta':{'openai/session':'PRIVATE_HOST_HINT'}})
        assert not result.json()['result']['isError'];time.sleep(.02)
    items=resolved(s,'activity_list')['activities']
    reads=[x for x in items if x['tool']=='read' and x['window_key']]
    assert len(reads)>=2 and any(x['next_call_gap_ms'] is not None for x in reads)
    assert all(x['service_ms'] is not None and x['status']!='running' for x in reads)
    assert 'PRIVATE_HOST_HINT' not in json.dumps(items)
    grant=s.client.post('/api/grants',json={'label':'other-observer','scopes':['read'],'projects':[s.project['id']],'days':1}).json()
    own=s.mcp('process',{'operation':'activity','project':'Imago'},token_value=grant['token'])['structuredContent']['activities']
    assert all(row['tool']=='process' and row['window_key'] is None for row in own)
    s.client.delete('/api/grants/'+grant['grant_id'])


def test_handoff_preserves_goal_and_does_not_start_execution(integrated_stack):
    s=integrated_stack
    created=s.mcp('workspace',{'project': 'Imago', 'idempotency_key': uuid.uuid4().hex, 'operation': 'workflow_create', 'options': {'title': 'Handoff fixture', 'goal': 'Keep original intent'}})['structuredContent']
    result=s.mcp('workspace',{'operation': 'handoff', 'options': {'workflow_id': created['workflow_id']}})['structuredContent']
    assert result['original_goal']=='Keep original intent' and not result['execution_started']
    assert result['remaining'] and result['next']['tool']=='workspace' and result['next']['arguments']['operation']=='workflow_get'


def test_local_owner_control_is_loopback_authenticated_and_journaled(integrated_stack):
    s=integrated_stack
    resolved(s,'readiness_get') # registers the authenticated mapping in the local catalogue
    path=s.directory/'agent-state'/'integration-local.json';descriptor=json.loads(path.read_text())
    url='http://127.0.0.1:'+str(descriptor['port'])
    headers={'Authorization':'Bearer '+descriptor['token']}
    with httpx.Client(base_url=url,trust_env=False,timeout=10) as client:
        assert client.post('/status',json={}).status_code==401
        assert client.post('/status',headers={**headers,'Origin':'https://evil.test'},json={}).status_code==403
        status=client.post('/status',headers=headers,json={});assert status.status_code==200 and status.json()['projects']
        args={'project':'Imago','action':'pause','confirm':'Imago','idempotency_key':uuid.uuid4().hex}
        first=client.post('/control',headers=headers,json=args).json()
        again=client.post('/control',headers=headers,json=args).json()
        assert first==again and first['data']['paused']
        resumed=client.post('/control',headers=headers,json={**args,'action':'resume','idempotency_key':uuid.uuid4().hex}).json()
        assert resumed['ok'] and not resumed['data']['paused']
