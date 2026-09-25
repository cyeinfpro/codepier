"""Live role grants: additions, revocation, rule pairing and delegated creation."""
import asyncio
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Request

from hub.roles import ROLE_SCOPE, role_project_scopes, require_role
from shared.crypto import digest, password_hash
from tests.test_audit_api import api as api
from tests.test_access_profiles import call, data, start_oauth, create as fixed_profile, pat as fixed_pat
from tests.test_continuous_access import add_project, grant_for, visible_projects


def must(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def role(client, project_rules=None, device_rules=None, **extra):
    body = {'label': 'secretary', 'project_rules': project_rules if project_rules is not None else [{'actions':['read'], 'projects':['project']}],
            'device_rules': device_rules or [], 'idempotency_key': uuid.uuid4().hex, **extra}
    return must(client.post('/api/access-roles', json=body), 201)


def update_role(client, current, **extra):
    body = {key: current[key] for key in ('label', 'enabled', 'project_rules', 'device_rules')}
    return client.put('/api/access-roles/' + current['id'], json={**body, 'expected_version': current['version'], **extra})


def profile(client, role, label='Secretary'):
    return must(client.post('/api/access-profiles', json={'label':label,'role_id':role['id'], 'idempotency_key':uuid.uuid4().hex}), 201)


def credential(client, role, profile, **extra):
    return client.post('/api/grants', json={'label':'secretary connection', 'scopes':[ROLE_SCOPE],
        'authorization_mode':'role','profile_id':profile['id'],'profile_version':profile['version'],
        'role_version':role['version'],'confirm_dynamic_role':True, **extra})


def setup_role(client, **kwargs):
    r = role(client, **kwargs); p = profile(client, r)
    return r, p, must(credential(client, r, p))


def error(response, code):
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert result['isError'], result
    value = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    assert value['error']['code'] == code, value
    assert 'mcp/www_authenticate' not in result.get('_meta', {})
    return value


def principal(app, token):
    return app.state.auth.bearer(Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())]}))


def test_live_project_and_scope_addition_keep_identity_and_grant(api):
    app, client, _ = api
    r,p,token = setup_role(client)
    before = grant_for(app, token['token'])
    assert visible_projects(client, token['token']) == {'project'}
    add_project(app)
    r = must(update_role(client,r,project_rules=[{'actions':['read','write','execute'],'projects':['project','future']}]))
    assert visible_projects(client, token['token']) == {'project','future'}
    context = data(call(client,token['token'],'get_access_context'))
    assert context['role']['version'] == r['version']
    assert set(context['scopes']) >= {'read','write','execute'}
    assert context['initial_consent_is_resource_ceiling'] is False
    assert data(call(client,token['token']))['id'] == p['id']
    assert grant_for(app,token['token']) == before


def test_future_projects_and_rule_exclusion_are_live(api):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[{'actions':['read'],'all_projects':True,'excluded_projects':['project']}])
    assert visible_projects(client,t['token']) == set()
    add_project(app)
    assert visible_projects(client,t['token']) == {'future'}
    assert data(call(client,t['token'],'get_access_context'))['project_permissions'][0]['actions'] == ['read']


def test_role_shared_by_distinct_profiles_not_shared_grants(api):
    app,client,_=api
    r,p,t=setup_role(client)
    deputy=profile(client,r,'Deputy'); t2=must(credential(client,r,deputy))
    add_project(app)
    must(update_role(client,r,project_rules=[{'actions':['read'],'all_projects':True}]))
    assert visible_projects(client,t['token']) == visible_projects(client,t2['token']) == {'project','future'}
    assert data(call(client,t['token']))['id'] != data(call(client,t2['token']))['id']
    assert t['grant_id'] != t2['grant_id']


def test_read_all_execute_one_never_cross_multiplies(api):
    app,client,_=api
    add_project(app)
    r,p,t=setup_role(client,project_rules=[{'actions':['read'],'all_projects':True},
                                         {'actions':['read','write','execute'],'projects':['project']}])
    who=principal(app,t['token'])
    assert role_project_scopes(app.state.store,who,'project') == {'read','write','execute'}
    assert role_project_scopes(app.state.store,who,'future') == {'read'}
    error(call(client,t['token'],'shell_exec',{'project':'future','command':'echo forbidden','idempotency_key':'must-not-run-001'}),'ROLE_POLICY_DENIED')
    error(call(client,t['token'],'fs_write',{'project':'future','path':'x.txt','content':'forbidden','expected_sha256':'new','idempotency_key':'must-not-write-01'}),'ROLE_POLICY_DENIED')
    assert app.state.store.one('SELECT count(*) AS n FROM operations')['n'] == 0


def test_paused_role_is_policy_denial_not_reauth_and_can_resume(api):
    app,client,_=api
    r,p,t=setup_role(client)
    paused=must(update_role(client,r,enabled=False))
    assert data(call(client,t['token']))['id'] == p['id']
    assert data(call(client,t['token'],'get_access_context'))['role']['enabled'] is False
    error(call(client,t['token'],'projects_list'),'ROLE_POLICY_DENIED')
    must(update_role(client,paused,enabled=True))
    assert visible_projects(client,t['token']) == {'project'}


def test_revoked_grant_stays_revoked_after_role_resume(api):
    app,client,_=api
    r,p,t=setup_role(client)
    must(client.delete('/api/grants/'+t['grant_id']))
    r=must(update_role(client,r,enabled=False));must(update_role(client,r,enabled=True))
    assert call(client,t['token']).status_code == 401


@pytest.mark.parametrize('extra', [{'confirm_dynamic_role':False},{'confirm_dynamic_role':1},{'role_version':999},
    {'profile_version':999},{'scopes':['read']},{'authorization_mode':'fixed'}, {'projects':['project']},{'all_projects':True}])
def test_role_consent_is_explicit_and_version_bound(api,extra):
    app,client,_=api
    r=role(client);p=profile(client,r)
    count=app.state.store.one('SELECT count(*) AS n FROM grants')['n']
    assert credential(client,r,p,**extra).status_code in {400,409,422}
    assert app.state.store.one('SELECT count(*) AS n FROM grants')['n'] == count


def test_old_fixed_grant_not_upgraded_by_attaching_role_or_editing_it(api):
    app,client,_=api
    p=fixed_profile(client);t=fixed_pat(client,p)
    r=role(client,project_rules=[{'actions':['read','write','execute'],'all_projects':True}])
    p=must(client.put('/api/access-profiles/'+p['id'],json={'label':p['label'],'scopes':['read'],
          'projects':['project'],'enabled':True,'role_id':r['id'],'expected_version':p['version']}))
    add_project(app)
    assert visible_projects(client,t['token']) == {'project'}
    assert data(call(client,t['token'],'get_access_context'))['authorization_mode'] == 'fixed'
    assert grant_for(app,t['token'])['role_id'] is None


def test_role_rebinding_does_not_silently_change_existing_identity_authority(api):
    app,client,_=api
    r,p,t=setup_role(client)
    other=role(client,label='other')
    must(client.put('/api/access-profiles/'+p['id'],json={'label':p['label'],'scopes':['read'],
        'projects':[],'role_id':other['id'],'enabled':True,'expected_version':p['version']}))
    assert call(client,t['token']).status_code == 401


def role_oauth(client,r,p):
    cid,rid,verifier=start_oauth(client,ROLE_SCOPE)
    decision=must(client.post('/api/oauth/requests/'+rid+'/decide',json={'allow':True,'scopes':[ROLE_SCOPE],
        'authorization_mode':'role','profile_id':p['id'],'profile_version':p['version'],
        'role_version':r['version'],'confirm_dynamic_role':True}))
    code=parse_qs(urlsplit(decision['redirect']).query)['code'][0]
    result=must(client.post('/oauth/token',data={'grant_type':'authorization_code','code':code,'client_id':cid,
        'redirect_uri':'http://localhost:12345/callback','code_verifier':verifier}))
    return cid,result


def test_role_oauth_refresh_keeps_coarse_scope_but_follows_new_projects(api):
    app,client,_=api
    r=role(client);p=profile(client,r);cid,t=role_oauth(client,r,p)
    assert t['scope'] == ROLE_SCOPE
    add_project(app)
    r=must(update_role(client,r,project_rules=[{'actions':['read','execute'],'all_projects':True}]))
    fresh=must(client.post('/oauth/token',data={'grant_type':'refresh_token','refresh_token':t['refresh_token'],'client_id':cid}))
    assert fresh['scope'] == ROLE_SCOPE
    assert grant_for(app,t['access_token'])['id'] == grant_for(app,fresh['access_token'])['id']
    assert visible_projects(client,t['access_token']) == visible_projects(client,fresh['access_token']) == {'project','future'}
    paused=must(update_role(client,r,enabled=False))
    rotated=must(client.post('/oauth/token',data={'grant_type':'refresh_token','refresh_token':fresh['refresh_token'],'client_id':cid}))
    assert rotated['scope']==ROLE_SCOPE
    error(call(client,rotated['access_token'],'projects_list'),'ROLE_POLICY_DENIED')
    must(update_role(client,paused,enabled=True))
    assert visible_projects(client,rotated['access_token'])=={'project','future'}


def test_scope_refresh_cannot_upgrade_legacy_to_role(api):
    from tests.test_audit_api import oauth_tokens
    app,client,_=api
    cid,t=oauth_tokens(client)
    response=client.post('/oauth/token',data={'grant_type':'refresh_token','refresh_token':t['refresh_token'],'client_id':cid,'scope':ROLE_SCOPE})
    assert response.status_code==400 and response.json()['error']=='invalid_scope'
    # Rejected scope change does not consume the original rotation token.
    assert client.post('/oauth/token',data={'grant_type':'refresh_token','refresh_token':t['refresh_token'],'client_id':cid}).status_code==200


def test_legacy_oauth_request_cannot_consent_role_even_with_profile(api):
    app,client,_=api
    r=role(client);p=profile(client,r);cid,rid,v=start_oauth(client)
    response=client.post('/api/oauth/requests/'+rid+'/decide',json={'allow':True,'scopes':[ROLE_SCOPE],
        'authorization_mode':'role','profile_id':p['id'],'profile_version':p['version'],'role_version':r['version'],'confirm_dynamic_role':True})
    assert response.status_code==400
    assert app.state.store.one('SELECT used FROM oauth_requests WHERE id=?',(rid,))['used']==0


@pytest.mark.parametrize('catalog',['full','coding'])
def test_role_catalog_oauth_schemes_and_entry_challenge(api,catalog):
    app,client,_=api
    r,p,t=setup_role(client)
    endpoint='/mcp?authorization=role&profile='+catalog
    unauth=client.post(endpoint,json={})
    assert ROLE_SCOPE in unauth.headers['WWW-Authenticate']
    result=must(client.post(endpoint,json={'jsonrpc':'2.0','id':1,'method':'tools/list'},headers={
        'Authorization':'Bearer '+t['token'],'Accept':'application/json, text/event-stream'}))['result']['tools']
    assert {'devices_list','projects_create','get_profile'} <= {tool['name'] for tool in result}
    for tool in result:
        assert tool['securitySchemes']==tool['_meta']['securitySchemes']==[{'type':'oauth2','scopes':[ROLE_SCOPE]}]
    assert next(tool for tool in result if tool['name']=='get_profile')['_meta']['openai/profile'] is True


def test_role_ui_endpoint_parameter_is_not_authority(api):
    app,client,fixed=api
    response=call(client,fixed,'shell_exec',{'project':'project','command':'echo no','idempotency_key':'old-not-role-01'},endpoint='/mcp?authorization=role')
    assert response.json()['result']['isError']
    assert grant_for(app,fixed)['authorization_mode']=='fixed'


def test_role_crud_requires_owner_csrf_origin_and_no_self_escalation(api):
    app,client,_=api
    r,p,t=setup_role(client)
    body={'label':'bad','project_rules':[],'idempotency_key':'role-unauthorized'}
    assert client.post('/api/access-roles',json=body,headers={'X-RD-CSRF':'wrong'}).status_code==403
    assert client.post('/api/access-roles',json=body,headers={'Origin':'https://other.example'}).status_code==403
    client.cookies.clear()
    assert client.post('/api/access-roles',json=body,headers={'Authorization':'Bearer '+t['token']}).status_code==401
    assert client.get('/api/access-roles',headers={'Authorization':'Bearer '+t['token']}).status_code==401
    error(call(client,t['token'],'roles_update',{'role_id':r['id']}),'UNKNOWN_TOOL')


def test_role_version_conflict_and_creation_idempotency(api):
    app,client,_=api
    body={'label':'secretary','project_rules':[],'idempotency_key':'role-stable-create'}
    first=must(client.post('/api/access-roles',json=body),201)
    assert must(client.post('/api/access-roles',json=body),201)['id']==first['id']
    assert client.post('/api/access-roles',json={**body,'label':'different'}).status_code==409
    latest=must(update_role(client,first,label='renamed'))
    assert latest['id']==first['id']
    assert update_role(client,first,enabled=False).status_code==409
    assert client.get('/api/access-roles/'+first['id']).json()['enabled'] is True


@pytest.mark.parametrize('rules',[ [{'actions':['write'],'all_projects':True}], [{'actions':['read','admin'],'all_projects':True}],
    [{'actions':['read'],'projects':['*']}], [{'actions':['read'],'projects':['missing']}],
    [{'actions':['read'],'all_projects':'true'}], [{'actions':['read'],'all_projects':True,'unexpected':True}] ])
def test_invalid_role_rules_fail_closed(api,rules):
    app,client,_=api
    response=client.post('/api/access-roles',json={'label':'bad','project_rules':rules,'idempotency_key':'invalid-role-001'})
    assert response.status_code in {400,422}
    assert app.state.store.one('SELECT count(*) AS n FROM access_roles')['n']==0


def test_corrupt_stored_role_does_not_fall_back_to_grant_snapshot(api):
    app,client,_=api
    r,p,t=setup_role(client)
    app.state.store.execute('UPDATE access_roles SET policy=? WHERE id=?',('{broken',r['id']))
    assert call(client,t['token']).status_code==401


def test_owner_cannot_bind_role_in_another_space(api):
    app,client,_=api
    sid=client.post('/api/iam/spaces',json={'label':'Other space','idempotency_key':'other-space-001'}).json()['id']
    r=must(client.post('/api/access-roles',headers={'X-CodePier-Space':sid},json={
        'label':'Other secretary','project_rules':[{'actions':['read'],'all_projects':True}],
        'idempotency_key':'other-role-create'}),201)
    assert client.post('/api/access-profiles',json={'label':'no','role_id':r['id'],'idempotency_key':'foreign-role-001'}).status_code==404


def test_queue_rechecks_paired_current_permission(api):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[{'actions':['read','execute'],'projects':['project']}])
    store=app.state.store;store.execute('UPDATE projects SET allow_tasks=1 WHERE id=?',('project',))
    receipt=data(call(client,t['token'],'shell_exec',{'project':'project','command':'echo no-run','idempotency_key':'queue-role-live'}))
    op=store.one('SELECT * FROM operations WHERE id=?',(receipt['operation_id'],));req=json.loads(store.decrypt(op['payload']))
    assert app.state.runtime.permission_error(op,req) is None
    add_project(app)
    must(update_role(client,r,project_rules=[{'actions':['read'],'all_projects':True},{'actions':['read','execute'],'projects':['future']}]))
    assert app.state.runtime.permission_error(op,req) is not None
    error(call(client,t['token'],'operations_get',{'operation_id':op['id']}),'ROLE_POLICY_DENIED')


def test_same_role_does_not_share_operation_ownership(api):
    app,client,_=api
    r,p,t=setup_role(client)
    other=must(credential(client,r,p))
    receipt=data(call(client,t['token'],'fs_read',{'project':'project','path':'README.md'}))
    error(call(client,other['token'],'operations_get',{'operation_id':receipt['operation_id']}),'OPERATION_NOT_FOUND')


def test_create_project_with_delegation_new_resource_and_same_token(api,monkeypatch):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[{'actions':['read','write'],'created_projects':True}],
                    device_rules=[{'actions':['devices.read','projects.create'],'devices':['device'],'max_project_mode':'write','root_prefixes':['/tmp/new-roots']}])
    assert visible_projects(client,t['token'])==set()
    devices=data(call(client,t['token'],'devices_list'))['devices']
    assert devices==[{'id':'device','name':'fixture','enabled':True,'online':False}]
    calls=[]
    async def validate(name,args,project,who,**kwargs):
        calls.append((name,who.admin))
        return {'root':project['root'],'writable':True,'allow_tasks':True}
    monkeypatch.setattr(app.state.runtime,'dispatch',validate)
    args={'alias':'new-work','device_id':'device','root':'/tmp/new-roots/work','mode':'write','idempotency_key':'role-create-work-1'}
    created=data(call(client,t['token'],'projects_create',args))
    assert created['created_by_role']==r['id'] and created['role_access']==['read','write']
    assert visible_projects(client,t['token'])=={created['id']}
    replay=data(call(client,t['token'],'projects_create',args))
    assert replay['id']==created['id'] and calls==[('system_validate',False)]
    assert app.state.store.one('SELECT count(*) AS n FROM role_created_projects')['n']==1
    error(call(client,t['token'],'projects_create',{**args,'alias':'changed'}),'IDEMPOTENCY_CONFLICT')


@pytest.mark.parametrize('change',[{'root':'/etc'}, {'mode':'write'}, {'allow_tasks':True}, {'device_id':'other'}])
def test_create_delegation_gates_before_agent_dispatch(api,monkeypatch,change):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[],device_rules=[{'actions':['devices.read','projects.create'],'devices':['device'],'root_prefixes':['/tmp/new-roots']}])
    async def forbidden(*args,**kwargs):
        pytest.fail('unauthorized request reached Agent dispatch')
    monkeypatch.setattr(app.state.runtime,'dispatch',forbidden)
    args={'alias':'x','device_id':'device','root':'/tmp/new-roots/x','idempotency_key':'create-denied-1',**change}
    error(call(client,t['token'],'projects_create',args),'ROLE_POLICY_DENIED')


def test_create_revocation_while_agent_validation_waits_prevents_commit(api,monkeypatch):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[],device_rules=[{'actions':['devices.read','projects.create'],'devices':['device']}])
    async def validate(name,args,project,who,**kwargs):
        app.state.store.execute('UPDATE access_roles SET enabled=0 WHERE id=?',(r['id'],))
        return {'root':project['root'],'writable':True,'allow_tasks':True}
    monkeypatch.setattr(app.state.runtime,'dispatch',validate)
    error(call(client,t['token'],'projects_create',{'alias':'blocked','device_id':'device','root':'/tmp/new','idempotency_key':'create-revoked-01'}),'ROLE_POLICY_DENIED')
    assert app.state.store.one("SELECT id FROM projects WHERE alias='blocked'") is None


def test_create_uses_agent_canonical_root_not_only_requested_path(api,monkeypatch):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[],device_rules=[{'actions':['devices.read','projects.create'],'devices':['device'],'root_prefixes':['/tmp/new-roots']}])
    async def validate(*args,**kwargs):
        return {'root':'/outside/canonical','writable':True,'allow_tasks':True}
    monkeypatch.setattr(app.state.runtime,'dispatch',validate)
    error(call(client,t['token'],'projects_create',{'alias':'blocked','device_id':'device','root':'/tmp/new-roots/link','idempotency_key':'canonical-check-1'}),'ROLE_POLICY_DENIED')
    assert app.state.store.one("SELECT id FROM projects WHERE alias='blocked'") is None


@pytest.mark.parametrize('root',['/tmp/fixture','/tmp/fixture/sub','/tmp'])
def test_create_cannot_alias_or_enclose_existing_restricted_project(api,monkeypatch,root):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[{'actions':['read'],'created_projects':True}],device_rules=[{'actions':['devices.read','projects.create'],'devices':['device']}])
    async def forbidden(*args,**kwargs):pytest.fail('overlap reached Agent')
    monkeypatch.setattr(app.state.runtime,'dispatch',forbidden)
    error(call(client,t['token'],'projects_create',{'alias':'alternate','device_id':'device','root':root,'idempotency_key':'overlap-denied-1'}),'PROJECT_ROOT_OVERLAP')


def test_pending_creation_retains_original_operation_and_reauthorizes_delivery(api):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[],device_rules=[{'actions':['devices.read','projects.create'],'devices':['device']}])
    args={'alias':'pending','device_id':'device','root':'/tmp/new-folder','idempotency_key':'pending-create-1'}
    first=error(call(client,t['token'],'projects_create',args),'VALIDATION_PENDING')
    second=error(call(client,t['token'],'projects_create',args),'VALIDATION_PENDING')
    assert first['error']['operation_id']==second['error']['operation_id']
    store=app.state.store;op=store.one('SELECT * FROM operations WHERE id=?',(first['error']['operation_id'],));req=json.loads(store.decrypt(op['payload']))
    assert app.state.runtime.permission_error(op,req) is None
    must(update_role(client,r,device_rules=[{'actions':['devices.read'],'devices':['device']}]))
    assert app.state.runtime.permission_error(op,req) is not None


def test_role_mode_cannot_invoke_owner_only_integration_controls(api):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[{'actions':['read','write','execute','computer'],'all_projects':True}])
    response=call(client,t['token'],'integration_control',{'project':'project'})
    assert response.json()['result']['isError']
    assert response.json()['result']['structuredContent']['error']['code']=='OWNER_REQUIRED'


def test_role_pat_consent_records_version_and_exact_policy(api):
    app,client,_=api
    r,p,t=setup_role(client)
    detail=json.loads(app.state.store.one("SELECT detail FROM audit WHERE action='role.consent' AND target=?",(t['grant_id'],))['detail'])
    assert detail['role_id']==r['id'] and detail['role_version']==r['version']
    assert detail['policy']['project_rules']==r['project_rules']
    assert detail['dynamic_resources_and_actions'] is True
    assert t['token'] not in json.dumps(detail)


def test_result_disclosure_rechecks_role_after_completion(api):
    from shared.util import DevError
    app,client,_=api
    r,p,t=setup_role(client)
    receipt=data(call(client,t['token'],'fs_read',{'project':'project','path':'README.md'}))
    original=principal(app,t['token'])
    must(update_role(client,r,enabled=False))
    with pytest.raises(DevError) as exc:
        app.state.runtime.authorized_result(receipt['operation_id'],{'ok':True,'data':{'content':'not disclosed'}},original)
    assert exc.value.code=='OPERATION_NOT_FOUND'
    assert exc.value.details['operation_id']==receipt['operation_id']


def test_settings_and_first_role_auth_challenge(api):
    app,client,_=api
    assert client.get('/api/settings').json()['role_mcp_url']=='http://testserver/mcp?authorization=role'
    for endpoint,scope in [('/mcp','read'),('/mcp?authorization=role',ROLE_SCOPE)]:
        reply=client.post(endpoint,headers={'Content-Type':'application/json'},json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        assert reply.status_code==401
        assert 'scope="'+scope+'"' in reply.headers['WWW-Authenticate']


def test_workflow_permissions_are_per_resource_even_in_update_metadata(api):
    app,client,_=api
    r,p,t=setup_role(client,project_rules=[{'actions':['read','write'],'projects':['project']}])
    created=data(call(client,t['token'],'workflows_create',{'project':'project','title':'Original task','goal':'Test exact resource policy',
                'idempotency_key':'workflow-role-pair-1'}))
    add_project(app)
    must(update_role(client,r,project_rules=[{'actions':['read'],'all_projects':True},{'actions':['read','write'],'projects':['future']}]))
    workflow=data(call(client,t['token'],'workflows_get',{'workflow_id':created['workflow_id']}))
    assert workflow['can_update'] is False
    error(call(client,t['token'],'workflows_update',{'workflow_id':created['workflow_id'],'expected_version':workflow['version'],
        'action':'checkpoint','summary':'Must not overwrite','idempotency_key':'workflow-role-update-1'}),'ROLE_POLICY_DENIED')


def test_schema6_upgrade_keeps_fixed_grants_profiles_and_master_key(api,tmp_path):
    import sqlite3
    from hub.store import Store
    from shared.crypto import token as secret_token
    directory=tmp_path/'old-store'
    from tests.legacy_iam_fixture import legacy_store
    old=legacy_store(directory)
    old.execute('INSERT INTO users VALUES (?,?,?,?)',('old-owner','old-owner',password_hash('test-owner-password'),time.time()))
    old.execute('INSERT INTO access_profiles(id,user_id,label,label_key,scopes,projects,enabled,version,created,updated,create_key,create_fingerprint) VALUES (?,?,?,?,?,?,1,1,?,?,?,?)',
        ('prf_old','old-owner','Old identity','old identity','["read"]','[]',time.time(),time.time(),'old-key','old-fingerprint'))
    old.execute('INSERT INTO grants(id,user_id,label,scopes,projects,revoked,created,profile_id) VALUES (?,?,?,?,?,0,?,?)',
        ('old-grant','old-owner','old grant','["read"]','[]',time.time(),'prf_old'))
    value=secret_token();encrypted=old.encrypt(value);key=(directory/'master.key').read_bytes();old.close()
    # Construct the actual pre-role schema: drop role references/columns/tables,
    # not merely a schema version value on an already upgraded database.
    with sqlite3.connect(directory/'hub.sqlite3') as db:
        db.execute('PRAGMA foreign_keys=OFF')
        for index in ('grants_role','profiles_role'):
            db.execute('DROP INDEX IF EXISTS '+index)
        for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall():
            if 'role_id' in row[1]: db.execute('DROP INDEX '+row[0])
        db.execute('ALTER TABLE grants DROP COLUMN authorization_mode')
        db.execute('ALTER TABLE grants DROP COLUMN role_id')
        db.execute('ALTER TABLE access_profiles DROP COLUMN role_id')
        db.execute('DROP TABLE role_created_projects');db.execute('DROP TABLE access_roles')
        db.execute("UPDATE meta SET value='6' WHERE key='schema'")
    new=Store(directory)
    try:
        assert new.one("SELECT value FROM meta WHERE key='schema'")['value']=='9'
        grant=new.one('SELECT * FROM grants WHERE id=?',('old-grant',))
        assert grant['authorization_mode']=='fixed' and grant['role_id'] is None
        assert grant['profile_id']=='prf_old' and grant['scopes']=='["read"]'
        assert new.one('SELECT role_id FROM access_profiles WHERE id=?',('prf_old',))['role_id'] is None
        assert new.one('SELECT count(*) AS n FROM access_roles')['n']==0
        assert new.decrypt(encrypted)==value and (directory/'master.key').read_bytes()==key
    finally:new.close()
