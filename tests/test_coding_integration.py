"""Actual loopback Hub/Agent coding tools, grants, replay and MCP schemas."""
import json
import hashlib
import uuid
import jsonschema
from shared.contracts import tool_definitions
from tests.support import running_stack
from tests.catalog_assertions import assert_task_catalog
import pytest


@pytest.fixture(scope='module')
def coding_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('coding-hub-agent')) as stack:
        yield stack


def rpc(s, method, params=None, token=None, profile='coding'):
    return s.client.post('/mcp?profile='+profile,headers={
        'Authorization':'Bearer '+(token or s.pat), 'Accept':'application/json, text/event-stream',
        'MCP-Protocol-Version':'2025-11-25'},json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})


def tool(s,name,args,token=None):
    from hub.core_tools import public_call
    if name == 'apply_patch': name = 'edit'
    else: name, args = public_call(name, args)
    response=rpc(s,'tools/call',{'name':name,'arguments':args},token)
    assert response.status_code==200,response.text
    result=response.json()['result']
    if not result['isError']:
        schema=next(t['outputSchema'] for t in tool_definitions('coding') if t['name']==name)
        jsonschema.validate(result['structuredContent'],schema)
    return result


def value(s,result):
    assert not result['isError'],result
    data=result['structuredContent']
    if data.get('pending'):
        operation=s.poll(data['operation_id'],timeout=20)
        assert operation['state']=='succeeded',operation
        return {'operation_id':operation['id'],**operation['result']['data']}
    return data


def test_profile_old_transport_and_default_full_remain_compatible(coding_stack):
    s=coding_stack
    original=s.rpc('tools/list').json()['result']['tools']
    compact=rpc(s,'tools/list').json()['result']['tools']
    assert_task_catalog(original, 9)
    assert_task_catalog(compact, 9)
    assert {t['name'] for t in compact} == {t['name'] for t in original}
    assert not {'integration_control','validations_accept'} & {t['name'] for t in original}
    initialized=rpc(s,'initialize',{'protocolVersion':'2025-11-25'}).json()['result']
    assert initialized['protocolVersion']=='2025-11-25'
    assert len(initialized['instructions'])<4000
    assert rpc(s,'tools/list',profile='unknown').status_code==400
    hidden=rpc(s,'tools/call',{'name':'computer_status','arguments':{'project':'Imago'}}).json()['result']
    assert hidden['isError'] and hidden['structuredContent']['error']['code']=='TOOL_REMOVED'


def test_remote_non_git_review_patch_idempotency_and_grant_boundaries(coding_stack):
    s=coding_stack
    first=value(s,tool(s,'open_workspace',{'project':'Imago','capture_baseline':True}))
    assert first['baseline_ref'] and first['workspace']['root']==str(s.imago)
    # Independent file requests preserve per-file failure and SHA checks.
    read = value(s, tool(s, 'read', {'project': 'Imago', 'path': 'README.md'}))
    assert read['sha256'] == hashlib.sha256((s.imago/'README.md').read_bytes()).hexdigest()
    assert tool(s, 'read', {'project': 'Imago', 'path': 'missing.txt'})['isError']
    again=value(s,tool(s,'open_workspace',{'project':'Imago','context_id':first['context_id']}))
    assert again['context_unchanged'] and again['context'] is None
    patch={'project':'Imago','idempotency_key':'coding-write-'+uuid.uuid4().hex,
           'changes':[{'path':'src/coding-test.py','expected_sha256':'new','content':'print("safe batch")\n'}]}
    applied=value(s,tool(s,'apply_patch',patch));replayed=value(s,tool(s,'apply_patch',patch))
    assert applied['operation_id']==replayed['operation_id'] and applied['success']
    changed=value(s,tool(s,'show_changes',{'project':'Imago','baseline_ref':first['baseline_ref']}))
    assert any(f['path']=='src/coding-test.py' for f in changed['files'])
    detail=value(s,tool(s,'show_changes',{'project':'Imago','review_ref':changed['review_ref'],'path':'src/coding-test.py'}))
    assert '+print("safe batch")' in detail['diff']
    (s.imago/'src/coding-test.py').write_text('later\n')
    fixed=value(s,tool(s,'show_changes',{'project':'Imago','review_ref':changed['review_ref'],'path':'src/coding-test.py'}))
    assert fixed['diff']==detail['diff']
    grant=s.must(s.client.post('/api/grants',json={'label':'coding-read-only','scopes':['read'],'projects':[s.project['id']],'days':1}))
    denied=tool(s,'apply_patch',patch,grant['token'])
    assert denied['isError'] and denied['structuredContent']['error']['code']=='INSUFFICIENT_SCOPE'
    private=tool(s,'show_changes',{'project':'Imago','review_ref':changed['review_ref']},grant['token'])
    assert private['isError'] and private['structuredContent']['error']['code']=='REVIEW_NOT_FOUND'
    # The reduced profile does not weaken expected-SHA validation either.
    conflict=tool(s,'apply_patch',{**patch,'idempotency_key':'new-intent-'+uuid.uuid4().hex})
    assert conflict['isError'] and conflict['structuredContent']['error']['code']=='SHA_CONFLICT'


def test_coding_profile_uses_canonical_oauth_resource_and_revocation(coding_stack):
    from urllib.parse import parse_qs,urlparse
    from tests.test_integration import oauth_prepare
    s=coding_stack
    challenge=s.client.post('/mcp?profile=coding',headers={'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
    assert challenge.status_code==401 and 'oauth-protected-resource/mcp' in challenge.headers['www-authenticate']
    assert s.client.get('/.well-known/oauth-protected-resource/mcp').json()['resource']==s.url+'/mcp'
    registration,request,verifier,args=oauth_prepare(s,'read')
    decision=s.must(s.client.post('/api/oauth/requests/'+request+'/decide',json={'allow':True,'scopes':['read'],'projects':[s.project['id']]}))
    code=parse_qs(urlparse(decision['redirect']).query)['code'][0]
    tokens=s.must(s.client.post('/oauth/token',data={'grant_type':'authorization_code','client_id':registration['client_id'],'code':code,'code_verifier':verifier,'redirect_uri':args['redirect_uri'],'resource':s.url+'/mcp'}))
    assert_task_catalog(rpc(s,'tools/list',token=tokens['access_token']).json()['result']['tools'], 9)
    opened=value(s,tool(s,'open_workspace',{'project':'Imago'},tokens['access_token']))
    assert opened['workspace']['granted_scopes']==['read']
    assert s.client.post('/oauth/revoke',data={'token':tokens['access_token'],'client_id':registration['client_id']}).status_code==200
    assert rpc(s,'tools/list',token=tokens['access_token']).status_code==401
