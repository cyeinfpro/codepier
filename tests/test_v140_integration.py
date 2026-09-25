from shared.util import VERSION
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
import httpx
import pytest
from tests.support import wait_for


def resolved(stack,tool_name,**args):
    result=stack.call(tool_name,{"project":"Imago",**args})
    if result.get('pending'):
        op=stack.poll(result['operation_id'],timeout=20)
        assert op['state']=='succeeded',op
        return {'operation_id':op['id'],**op['result']['data']}
    return result


def test_actual_artifact_snapshot_and_range_downloads(stack,tmp_path):
    content=bytes(range(256))*4097
    filename='deliverable-'+uuid.uuid4().hex+'.zip';(stack.imago/filename).write_bytes(content)
    artifact=resolved(stack,'artifacts_register',path=filename,name='完整代码包.zip',idempotency_key=uuid.uuid4().hex)
    path=artifact['download_path'];identifier=artifact['artifact_id']
    assert artifact['sha256']==hashlib.sha256(content).hexdigest()
    with httpx.Client() as anon:assert anon.get(stack.url+path).status_code==401
    full=stack.client.get(path)
    assert full.status_code==200 and full.content==content
    assert 'filename*=UTF-8' in full.headers['content-disposition']
    assert stack.client.head(path).headers['content-length']==str(len(content))
    part=stack.client.get(path,headers={'Range':'bytes=123-300000','If-Range':full.headers['etag']})
    assert part.status_code==206 and part.content==content[123:300001]
    suffix=stack.client.get(path,headers={'Range':'bytes=-71'});assert suffix.content==content[-71:]
    assert stack.client.get(path,headers={'Range':'bytes=99999999-'}).status_code==416
    assert stack.client.get(path,headers={'Range':'bytes=1-3','If-Range':'"old"'}).content==content
    (stack.imago/filename).write_bytes(b'changed-source')
    assert stack.client.get(path).content==content
    rows=stack.call('artifacts_list',{'project':'Imago'})['artifacts']
    assert any(a['artifact_id']==identifier for a in rows)
    from scripts.download_artifact import download
    token_file=tmp_path/'token.txt';token_file.write_text(stack.pat);token_file.chmod(0o600)
    # Panel-created artifacts do not implicitly transfer ownership to a MCP grant.
    with pytest.raises(httpx.HTTPStatusError):download(stack.url,identifier,token_file,tmp_path/'denied.zip')
    mcp=stack.mcp('write',{'project': 'Imago', 'idempotency_key': uuid.uuid4().hex, 'operation': 'artifact', 'options': {'path': filename}})
    assert not mcp['isError'];metadata=mcp['structuredContent']
    if metadata.get('pending'):
        op=stack.poll(metadata['operation_id']);metadata=op['result']['data']
    result=download(stack.url,metadata['artifact_id'],token_file,tmp_path/'verified.zip')
    assert result['verified'] and (tmp_path/'verified.zip').read_bytes()==b'changed-source'


def test_artifact_zero_bytes_and_offline_resume(stack):
    filename='empty-'+uuid.uuid4().hex; (stack.imago/filename).write_bytes(b'')
    empty=resolved(stack,'artifacts_register',path=filename,idempotency_key=uuid.uuid4().hex)
    response=stack.client.get(empty['download_path']);assert response.status_code==200 and response.content==b''
    assert stack.client.get(empty['download_path'],headers={'Range':'bytes=0-0'}).status_code==416
    filename='offline-'+uuid.uuid4().hex;(stack.imago/filename).write_bytes(b'offline-safe')
    artifact=resolved(stack,'artifacts_register',path=filename,idempotency_key=uuid.uuid4().hex)
    stack.stop_agent()
    try:
        wait_for(lambda:not next(d for d in stack.client.get('/api/devices').json()['devices'] if d['id']==stack.device)['online'])
        assert stack.client.get(artifact['download_path']).status_code==503
    finally:stack.start_agent()
    assert stack.client.get(artifact['download_path'],headers={'Range':'bytes=4-'}).content==b'ine-safe'


def test_true_agent_search_sessions_diagnostics_and_restart(stack):
    filename='symbols-'+uuid.uuid4().hex+'.ts'
    (stack.imago/filename).write_text('export function searchTarget(){ return 1; }\nexport const caller = () => searchTarget();\n')
    found=resolved(stack,'code_symbols',path=filename)
    assert [s['name'] for s in found['symbols']]==['searchTarget','caller']
    search=resolved(stack,'searches_start',query='searchTarget',mode='references',file_glob=filename)
    page=resolved(stack,'searches_get',search_id=search['search_id'])
    assert len(page['results'])==1 and page['results'][0]['line']==2
    stack.stop_agent();stack.start_agent()
    (stack.imago/filename).write_text('export const changed = 1;')
    stale=resolved(stack,'searches_get',search_id=search['search_id'])
    assert stale['results'][0]['stale']
    info=stack.call('diagnostics_get',{'project':'Imago'})
    assert info['hub']['runtime']['version']==VERSION
    assert info['devices'][0]['reported_version']==VERSION
    assert info['devices'][0]['missing_tools']==[]
    trace=stack.call('operations_trace',{'operation_id':found['operation_id']})
    assert any(e['stage']=='executing' for e in trace['events'])
    assert any(e['stage']=='hub_completed' for e in trace['events'])
    with httpx.Client() as c:
        r=c.get(stack.url+'/api/artifacts/'+uuid.uuid4().hex)
        assert r.status_code==401


def test_search_and_artifact_do_not_cross_grants(stack):
    name='scope-'+uuid.uuid4().hex+'.txt';(stack.imago/name).write_text('needle')
    issued=stack.client.post('/api/grants',json={'label':'isolated-v140','scopes':['read','write'],'projects':[stack.project['id']],'days':1}).json()
    search=stack.mcp('process',{'project': 'Imago', 'operation': 'search_start', 'options': {'query': 'needle', 'file_glob': name}})['structuredContent']
    if search.get('pending'):
        op=stack.poll(search['operation_id']);search=op['result']['data']
    other=stack.mcp('process',{'project': 'Imago', 'operation': 'search_get', 'options': {'search_id': search['search_id']}},token_value=issued['token'])
    assert other['isError'] and other['structuredContent']['error']['code']=='OPERATION_NOT_FOUND'
    artifact=stack.mcp('write',{'project': 'Imago', 'idempotency_key': uuid.uuid4().hex, 'operation': 'artifact', 'options': {'path': name}})['structuredContent']
    if artifact.get('pending'):artifact=stack.poll(artifact['operation_id'])['result']['data']
    with httpx.Client() as client:
        rejected=client.get(stack.url+artifact['download_path'],headers={'Authorization':'Bearer '+issued['token']})
        assert rejected.status_code==404
    stack.client.delete('/api/grants/'+issued['grant_id'])


def test_cli_resumes_partial_and_rejects_corrupt_prefix(stack,tmp_path):
    from scripts.download_artifact import download
    content=bytes(range(251))*5000;name='resume-'+uuid.uuid4().hex+'.bin';(stack.imago/name).write_bytes(content)
    result=stack.mcp('write',{'project': 'Imago', 'idempotency_key': uuid.uuid4().hex, 'operation': 'artifact', 'options': {'path': name}})['structuredContent']
    if result.get('pending'):result=stack.poll(result['operation_id'])['result']['data']
    identity={k:result[k] for k in ('artifact_id','sha256','bytes')}
    token=tmp_path/'private-token.txt';token.write_text(stack.pat);token.chmod(0o600)
    target=tmp_path/'resumed.bin'
    target.with_name(target.name+'.part').write_bytes(content[:57123])
    target.with_name(target.name+'.part.json').write_text(json.dumps(identity))
    assert download(stack.url,identity['artifact_id'],token,target)['verified']
    assert target.read_bytes()==content and not target.with_name(target.name+'.part').exists()
    bad=tmp_path/'corrupt.bin'
    bad.with_name(bad.name+'.part').write_bytes(b'bad-prefix')
    bad.with_name(bad.name+'.part.json').write_text(json.dumps(identity))
    with pytest.raises(ValueError,match='SHA-256'):download(stack.url,identity['artifact_id'],token,bad)
    assert not bad.exists()
