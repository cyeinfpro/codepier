"""Real HTTP/database multi-user authorization. All records are disposable."""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from fastapi import Request
from hub.app import create_app
from hub import iam
from shared.crypto import password_hash
from shared.util import DevError
from tests.test_roles import role, profile, credential, must, update_role
from tests.test_access_profiles import call, data


@dataclass
class Browser:
    client: TestClient
    cookie: str
    csrf: str
    space: str

    def request(self, method, path, **kwargs):
        headers={'Cookie':'rd_session='+self.cookie,'X-RD-CSRF':self.csrf,'X-CodePier-Space':self.space}
        headers.update(kwargs.pop('headers',{}))
        return self.client.request(method,path,headers=headers,**kwargs)

    def get(self,path,**kwargs):return self.request('GET',path,**kwargs)
    def post(self,path,**kwargs):return self.request('POST',path,**kwargs)
    def put(self,path,**kwargs):return self.request('PUT',path,**kwargs)
    def patch(self,path,**kwargs):return self.request('PATCH',path,**kwargs)
    def delete(self,path,**kwargs):return self.request('DELETE',path,**kwargs)


@pytest.fixture
def team(tmp_path,monkeypatch):
    monkeypatch.setenv('HUB_PUBLIC_URL','http://testserver')
    monkeypatch.setenv('MCP_PUBLIC_URL','')
    app=create_app(str(tmp_path/'hub'))
    store=app.state.store
    hashed=password_hash('fixture-password-only')
    sessions={}
    with store.transaction():
        for uid in ('owner','alice','bob'):
            store.db.execute('INSERT INTO users VALUES(?,?,?,?)',(uid,uid,hashed,time.time()))
            if uid!='owner':
                store.db.execute('DELETE FROM memberships WHERE user_id=?',(uid,))
                iam.create_personal_space(store,uid,uid+' personal')
            sessions[uid]=app.state.auth.new_session(uid)
        store.db.execute("INSERT INTO spaces(id,label,kind,created) VALUES('team','Development','team',?)",(time.time(),))
        for uid in sessions:
            store.db.execute('INSERT INTO memberships(space_id,user_id,level) VALUES(?,?,?)',('team',uid,'owner' if uid=='owner' else 'member'))
        for sid,uid in [('team','owner'),('legacy','owner')]:
            device='device-'+sid
            store.db.execute('INSERT INTO devices(id,name,secret,space_id,owner_user_id,created) VALUES(?,?,?,?,?,?)',(device,device,store.encrypt('x'*43),sid,uid,time.time()))
            store.db.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,space_id,owner_user_id,created) VALUES(?,?,?,?,?,?,?,?,?,?)',('project-'+sid,'same-alias','same-alias',device,str(tmp_path/sid),'write',1,sid,uid,time.time()))
    with TestClient(app,raise_server_exceptions=True) as client:
        browsers={uid:Browser(client,info['cookie'],info['csrf'],'team') for uid,info in sessions.items()}
        browsers['legacy']=Browser(client,sessions['owner']['cookie'],sessions['owner']['csrf'],'legacy')
        yield app,browsers


def assign(owner,r,uid,delegate=True):
    return must(owner.put('/api/iam/spaces/team/assignments/'+r['id']+'/'+uid,json={'active':True,'may_delegate':delegate,'expected_version':0}))


def shared_role(app,browsers,actions=None):
    r=role(browsers['owner'],project_rules=[{'actions':actions or ['read'],'all_projects':True}],label='secretary')
    for uid in ('alice','bob'):assign(browsers['owner'],r,uid)
    return r


def test_new_user_is_not_instance_admin_or_legacy_space_member(team):
    app,b=team
    me=must(b['alice'].get('/api/iam/me'))
    assert not me['instance_admin'] and {s['id'] for s in me['spaces']}=={'team',next(s['id'] for s in me['spaces'] if s['kind']=='personal')}
    assert b['alice'].get('/api/iam/users').status_code==403
    assert b['alice'].get('/api/projects',headers={'X-CodePier-Space':'legacy'}).status_code==403
    assert 'project-legacy' not in b['alice'].get('/api/projects').text


def test_shared_role_expands_projects_and_capabilities_on_same_mcp_token(team):
    app,b=team;r=shared_role(app,b)
    p=profile(b['alice'],r,'Alice secretary');grant=must(credential(b['alice'],r,p));token=grant['token']
    ctx=data(call(b['alice'],token,'get_access_context'))
    assert {x['id'] for x in ctx['projects']}=={'project-team'}
    store=app.state.store
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,space_id,owner_user_id,created) VALUES('new-project','new','new','device-team','/tmp/new','team','owner',?)",(time.time(),))
    r=must(update_role(b['owner'],r,project_rules=[{'actions':['read','write','execute'],'all_projects':True}]))
    ctx=data(call(b['alice'],token,'get_access_context'))
    assert {x['id'] for x in ctx['projects']}=={'project-team','new-project'}
    assert all('execute' in x['actions'] for x in ctx['project_permissions'])
    assert data(call(b['alice'],token,'get_profile'))['id']==p['id']
    assert app.state.store.one('SELECT id FROM grants WHERE id=?',(grant['grant_id'],))
    # Client hints cannot retarget the fixed Space binding.
    out=call(b['alice'],token,'get_access_context',endpoint='/mcp?space_id=legacy')
    assert {x['id'] for x in data(out)['projects']}=={'project-team','new-project'}


def test_role_use_assignment_does_not_imply_permission_to_delegate(team):
    app,b=team
    r=role(b['owner'],project_rules=[{'actions':['read'],'all_projects':True}])
    assign(b['owner'],r,'alice',False)
    assert len(must(b['alice'].get('/api/projects'))['projects'])==1
    response=b['alice'].post('/api/access-profiles',json={'label':'escalation','role_id':r['id'],'idempotency_key':uuid.uuid4().hex})
    assert response.status_code==403
    assert b['alice'].post('/api/access-roles',json={'label':'admin','project_rules':[{'actions':['read','execute'],'all_projects':True}],'idempotency_key':uuid.uuid4().hex}).status_code==403


def test_shared_role_keeps_other_users_profiles_and_grants_private(team):
    app,b=team;r=shared_role(app,b)
    pa=profile(b['alice'],r,'SECRET_ALICE_PROFILE');ga=must(credential(b['alice'],r,pa))
    pb=profile(b['bob'],r,'Bob');gb=must(credential(b['bob'],r,pb))
    assert 'SECRET_ALICE_PROFILE' not in b['bob'].get('/api/access-profiles').text
    assert ga['grant_id'] not in b['bob'].get('/api/grants').text
    assert b['bob'].get('/api/access-profiles/'+pa['id']).status_code==404
    assert b['bob'].delete('/api/grants/'+ga['grant_id']).status_code==200
    assert not app.state.store.one('SELECT revoked FROM grants WHERE id=?',(ga['grant_id'],))['revoked']
    # A credential authenticates its owner, not the panel cookie supplied beside it.
    assert data(call(b['bob'],ga['token'],'get_profile'))['id']==pa['id']
    assert data(call(b['alice'],gb['token'],'get_profile'))['id']==pb['id']


def test_no_cross_space_alias_or_action_product(team):
    app,b=team;r=shared_role(app,b)
    r=must(update_role(b['owner'],r,project_rules=[{'actions':['read'],'all_projects':True},{'actions':['read','execute'],'projects':['project-team']}]))
    token=must(credential(b['alice'],r,profile(b['alice'],r)))['token']
    resolved=data(call(b['alice'],token,'projects_resolve',{'project':'same-alias'}))
    assert resolved['id']=='project-team'
    denied=call(b['alice'],token,'projects_resolve',{'project':'project-legacy'})
    assert denied.json()['result']['isError'] and 'project-legacy' not in json.dumps(denied.json().get('result',{}).get('structuredContent',{}).get('project',{}))


def test_assignment_membership_and_user_revocation_stop_original_credentials(team):
    app,b=team;r=shared_role(app,b)
    p=profile(b['alice'],r);g=must(credential(b['alice'],r,p))
    assignment=app.state.store.one("SELECT * FROM role_assignments WHERE user_id='alice' AND role_id=?",(r['id'],))
    assert call(b['alice'],g['token']).status_code==200
    must(b['owner'].put('/api/iam/spaces/team/assignments/'+r['id']+'/alice',json={'active':False,'may_delegate':True,'expected_version':assignment['version']}))
    assert call(b['alice'],g['token']).status_code==403
    must(b['owner'].put('/api/iam/spaces/team/assignments/'+r['id']+'/alice',json={'active':True,'may_delegate':True,'expected_version':assignment['version']+1}))
    assert call(b['alice'],g['token']).status_code==200
    must(b['owner'].put('/api/iam/spaces/team/members/alice/suspension',json={'blocked':True}))
    assert call(b['alice'],g['token']).status_code==403
    must(b['owner'].put('/api/iam/spaces/team/members/alice/suspension',json={'blocked':False}))
    row=app.state.store.one("SELECT version FROM iam_users WHERE user_id='alice'")
    must(b['owner'].put('/api/iam/users/alice',json={'active':False,'instance_admin':False,'expected_version':row['version']}))
    assert call(b['alice'],g['token']).status_code==401
    assert not app.state.store.one("SELECT 1 AS ok FROM sessions WHERE user_id='alice'")
    assert app.state.store.one('SELECT revoked FROM grants WHERE id=?',(g['grant_id'],))['revoked']==1


def test_sql_rejects_cross_space_project_device_and_profile_role(team):
    import sqlite3
    app,b=team;store=app.state.store;r=shared_role(app,b)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,space_id,created) VALUES('bad','bad','bad','device-legacy','/tmp/bad','team',1)")
    p=profile(b['alice'],r)
    with pytest.raises(sqlite3.IntegrityError):store.execute("UPDATE access_profiles SET space_id='legacy' WHERE id=?",(p['id'],))
    assert store.all('PRAGMA foreign_key_check')==[]


def test_same_session_tabs_select_spaces_independently(team):
    app,b=team
    assert must(b['owner'].get('/api/projects'))['projects'][0]['id']=='project-team'
    assert must(b['legacy'].get('/api/projects'))['projects'][0]['id']=='project-legacy'
    assert must(b['owner'].get('/api/projects'))['projects'][0]['id']=='project-team'
    assert b['owner'].cookie==b['legacy'].cookie


def test_space_suspend_restore_and_last_owner_guard(team):
    app,b=team;owner=b['owner']
    denied=owner.put('/api/iam/spaces/team/members/owner',json={'level':'member','active':True,'expected_version':1})
    assert denied.status_code==409 and denied.json()['error']['code']=='LAST_OWNER'
    must(owner.put('/api/iam/spaces/team',json={'label':'Dev','active':False,'expected_version':1}))
    assert owner.get('/api/projects').status_code==404
    assert any(s['id']=='team' for s in must(owner.get('/api/iam/me'))['disabled_spaces'])
    assert b['alice'].post('/api/iam/spaces/team/restore',json={'label':'Dev','active':True,'expected_version':2}).status_code==404
    must(owner.post('/api/iam/spaces/team/restore',json={'label':'Dev','active':True,'expected_version':2}))
    assert owner.get('/api/projects').status_code==200


def test_overview_operations_audit_and_events_do_not_leak_other_humans(team):
    app,b=team;r=shared_role(app,b)
    p=profile(b['alice'],r);g=must(credential(b['alice'],r,p))
    store=app.state.store;now=time.time()
    store.execute("INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,result,created,updated,space_id,owner_user_id) VALUES('private-op','device-team','project-team','mcp:alice',?,'fs_read','{}','','succeeded',?,?,?,'team','alice')",(g['grant_id'],json.dumps({'ok':True,'data':{'text':'PRIVATE_ALICE_TEXT'}}),now,now))
    store.execute("INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id) VALUES(?,'panel:alice','PRIVATE_ALICE_EVENT','project-team','ok','{}','team','alice')",(now,))
    for path in ['/api/overview','/api/operations','/api/audit','/api/audit-export']:
        response=b['bob'].get(path);assert response.status_code==200,response.text
        assert 'PRIVATE_ALICE' not in response.text and 'private-op' not in response.text
    assert b['bob'].get('/api/operations/private-op').status_code==404
    req=Request({'type':'http','method':'GET','path':'/api/events','query_string':b'', 'headers':[(b'cookie',('rd_session='+b['bob'].cookie).encode()),(b'x-codepier-space',b'team')]})
    principal=app.state.auth.panel(req)
    assert not iam.event_visible(app.state.runtime,principal,{'type':'operation','data':{'id':'private-op'}})
    assert b['alice'].get('/api/operations/private-op').status_code==200


def test_invite_is_one_use_revocable_and_never_personal_space_sharing(team):
    app,b=team;owner=b['owner']
    invite=must(owner.post('/api/iam/spaces/team/invites',json={'level':'guest','days':1}),201)
    assert b['alice'].post('/api/iam/invites/accept',json={'invitation':invite['invitation']}).status_code==200
    assert b['bob'].post('/api/iam/invites/accept',json={'invitation':invite['invitation']}).status_code==400
    entries=must(owner.get('/api/iam/spaces/team/invites'))['invitations']
    assert 'invitation' not in entries[0]
    assert owner.delete('/api/iam/spaces/team/invites/'+entries[0]['id']).status_code==200
    personal=next(s['id'] for s in must(b['alice'].get('/api/iam/me'))['spaces'] if s['kind']=='personal')
    assert b['alice'].post('/api/iam/spaces/'+personal+'/invites',headers={'X-CodePier-Space':personal},json={}).status_code==403


def test_csrf_origin_and_no_unauthenticated_ordinary_user_access(team):
    app,b=team
    assert b['alice'].post('/api/iam/spaces',json={'label':'bad','idempotency_key':'test-key1'},headers={'X-RD-CSRF':'wrong'}).status_code==403
    assert b['alice'].post('/api/iam/spaces',json={'label':'bad','idempotency_key':'test-key2'},headers={'Origin':'https://evil.example'}).status_code==403
    for path in ['/api/iam/me','/api/iam/spaces','/api/iam/users','/api/projects','/api/audit']:
        assert b['alice'].get(path,headers={'Cookie':''}).status_code==401
