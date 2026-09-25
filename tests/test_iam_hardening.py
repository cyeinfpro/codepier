"""Cross-Space replay and late-revocation regression tests on real IAM records."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
import time
import uuid

from fastapi import Request
import pytest

from hub import iam
from hub.native_cli import make_native_router
from hub.store import Store
from shared.native_cli import database
from shared.util import DevError
from tests.test_iam_integration import team as team, shared_role, Browser
from tests.test_roles import must, profile, credential
from tests.test_access_profiles import call
from tests.test_oidc_integration import oidc as oidc, login


def request_for(browser, path='/api/native/sessions'):
    return Request({'type':'http','method':'GET','path':path,'query_string':b'',
                    'headers':[(b'cookie',('rd_session='+browser.cookie).encode()),
                               (b'x-codepier-space',browser.space.encode())]})


def tool(browser,name,args):
    return browser.post('/api/tools/call',json={'tool':name,'arguments':args})


def test_operation_request_key_is_scoped_to_space_and_replay_stays_stable(team):
    app,b=team
    args={'project':'same-alias','path':'README.md','idempotency_key':'shared-tab-operation-key'}
    left=must(tool(b['owner'],'fs_read',args));right=must(tool(b['legacy'],'fs_read',args))
    assert left['operation_id']!=right['operation_id']
    assert must(tool(b['owner'],'fs_read',args))['operation_id']==left['operation_id']
    assert must(tool(b['legacy'],'fs_read',args))['operation_id']==right['operation_id']
    rows=app.state.store.all('SELECT space_id FROM operations WHERE idem=?',(args['idempotency_key'],))
    assert {r['space_id'] for r in rows}=={'team','legacy'}
    assert b['owner'].get('/api/operations/'+right['operation_id']).status_code==404


def test_workflow_request_key_is_scoped_to_space_and_survives_restart(team):
    app,b=team
    args={'project':'same-alias','title':'Review','goal':'Test only','idempotency_key':'shared-tab-workflow-key'}
    left=must(tool(b['owner'],'workflows_create',args));right=must(tool(b['legacy'],'workflows_create',args))
    assert left['workflow_id']!=right['workflow_id']
    assert must(tool(b['owner'],'workflows_create',args))['replayed']
    assert must(tool(b['legacy'],'workflows_create',args))['replayed']
    # Opening the migrated DB again must NOT recreate the obsolete global index.
    second=Store(app.state.store.directory)
    try:
        assert len(second.all('SELECT * FROM workflow_replays WHERE idem=?',(args['idempotency_key'],)))==2
        assert second.one("SELECT value FROM meta WHERE key='schema'")['value']=='9'
        assert second.all('PRAGMA foreign_key_check')==[]
    finally:second.close()


@pytest.mark.parametrize('table,identifier',[('devices','device-team'),('projects','project-team')])
def test_parent_resource_space_cannot_be_changed_under_children(team,table,identifier):
    app,_=team
    with pytest.raises(sqlite3.IntegrityError,match='Space is immutable'):
        app.state.store.execute(f"UPDATE {table} SET space_id='legacy' WHERE id=?",(identifier,))
    assert app.state.store.one(f'SELECT space_id FROM {table} WHERE id=?',(identifier,))['space_id']=='team'


def test_suspended_inviter_cannot_admit_new_members(team):
    app,b=team
    store=app.state.store
    store.execute("UPDATE memberships SET level='admin' WHERE space_id='team' AND user_id='alice'")
    invitation=must(b['alice'].post('/api/iam/spaces/team/invites',json={}),201)['invitation']
    version=store.one("SELECT version FROM iam_users WHERE user_id='alice'")['version']
    must(b['owner'].put('/api/iam/users/alice',json={'active':False,'instance_admin':False,'expected_version':version}))
    result=b['bob'].post('/api/iam/invites/accept',json={'invitation':invitation})
    assert result.status_code==400 and result.json()['error']['code']=='INVITATION_INVALID'


def test_space_admin_cannot_suspend_an_owner_even_with_another_owner(team):
    app,b=team;store=app.state.store
    store.execute("UPDATE memberships SET level='admin' WHERE space_id='team' AND user_id='alice'")
    store.execute("UPDATE memberships SET level='owner' WHERE space_id='team' AND user_id='bob'")
    result=b['alice'].put('/api/iam/spaces/team/members/owner/suspension',json={'blocked':True})
    assert result.status_code==403 and result.json()['error']['code']=='OWNER_REQUIRED'
    assert not store.one("SELECT 1 FROM membership_blocks WHERE space_id='team' AND user_id='owner'")


def test_last_local_recovery_admin_cannot_be_demoted_with_only_oidc_admin_remaining(team):
    app,b=team;store=app.state.store
    store.execute("UPDATE iam_users SET instance_admin=1,local_login=0 WHERE user_id='alice'")
    version=store.one("SELECT version FROM iam_users WHERE user_id='owner'")['version']
    result=b['owner'].put('/api/iam/users/owner',json={'active':True,'instance_admin':False,'expected_version':version})
    assert result.status_code==409 and result.json()['error']['code']=='LAST_RECOVERY_ADMIN'
    assert store.one("SELECT instance_admin FROM iam_users WHERE user_id='owner'")['instance_admin']==1


def test_device_owner_can_manage_lifecycle_without_space_admin(team,monkeypatch):
    app,b=team;store=app.state.store;runtime=app.state.runtime
    store.execute("UPDATE devices SET owner_user_id='alice',info=? WHERE id='device-team'",(json.dumps({'device_actions':['agent_restart']}),))
    monkeypatch.setattr(runtime,'online',lambda _:True)
    calls=[]
    async def dispatch(name,args,project,principal,**kwargs):
        calls.append((name,principal.user_id,principal.admin))
        return {'pending':True,'operation_id':'fixture-operation'}
    monkeypatch.setattr(runtime,'dispatch',dispatch)
    alice=app.state.auth.panel(request_for(b['alice']))
    assert not alice.admin
    out=asyncio.run(runtime.dispatch_device_action('agent_restart',{'idempotency_key':'owner-restart'},'device-team',alice))
    assert out['pending'] and calls==[('agent_restart','alice',False)]
    bob=app.state.auth.panel(request_for(b['bob']))
    with pytest.raises(DevError):
        asyncio.run(runtime.dispatch_device_action('agent_restart',{},'device-team',bob))
    assert len(calls)==1


def test_pending_receipt_revalidates_membership_after_wait(team,monkeypatch):
    app,b=team;r=shared_role(app,b)
    runtime=app.state.runtime;principal=app.state.auth.panel(request_for(b['alice']))
    project=runtime.project('project-team',principal)
    runtime.wait_seconds=.01
    monkeypatch.setattr(runtime,'online',lambda _:True)
    original=runtime.store.one
    # Revoke in the asynchronous waiting interval, not before submission.
    async def run():
        async def revoke():
            await asyncio.sleep(.002)
            runtime.store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
        revocation=asyncio.create_task(revoke())
        try:
            with pytest.raises(DevError) as caught:
                await runtime.dispatch('fs_read',{'project':'same-alias','path':'a','idempotency_key':'pending-then-revoke'},project,principal)
            assert caught.value.code=='SPACE_FORBIDDEN'
            identifier=caught.value.details['operation_id']
            assert original('SELECT id FROM operations WHERE id=?',(identifier,))
        finally:await revocation
    asyncio.run(run())


def populate_native(app,owner='alice'):
    sid=uuid.uuid4().hex;store=app.state.store
    p=store.one("SELECT * FROM projects WHERE id='project-team'")
    store.execute('INSERT INTO native_ownership(id,space_id,owner_user_id,project_id,device_id,created) VALUES(?,?,?,?,?,?)',
                  (sid,'team',owner,p['id'],p['device_id'],time.time()))
    events=[{'type':'message','text':'FIRST_PRIVATE_TEXT','receipt':'one'},
            {'type':'message','text':'SECOND_PRIVATE_TEXT','receipt':'two'}]
    raw=b''.join((json.dumps(e)+'\n').encode() for e in events)
    with closing(database(app.state.runtime.native.directory)) as db,db:
        db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (sid,p['id'],p['device_id'],p['root'],p['root'],'codex','Private','exited',1,1,'chat'))
        db.execute('INSERT INTO output VALUES(?,?,?)',(sid,0,raw))
    return sid


@pytest.mark.parametrize('format',['json','md'])
def test_native_export_rechecks_between_items_not_just_batches(team,format):
    app,b=team;shared_role(app,b,['read','write','execute']);sid=populate_native(app)
    router=make_native_router(app.state.auth,app.state.runtime)
    endpoint=next(r.endpoint for r in router.routes if r.path.endswith('/export'))
    async def run():
        response=await endpoint(sid,request_for(b['alice']),format)
        iterator=response.body_iterator
        try:
            await anext(iterator) # format header
            first=await anext(iterator)
            assert 'FIRST_PRIVATE_TEXT' in first
            app.state.store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
            with pytest.raises(DevError):await anext(iterator)
        finally:await iterator.aclose()
    asyncio.run(run())


def test_native_event_replay_rechecks_between_frames(team):
    app,b=team;shared_role(app,b,['read','write','execute']);sid=populate_native(app)
    router=make_native_router(app.state.auth,app.state.runtime)
    endpoint=next(r.endpoint for r in router.routes if r.path.endswith('/events'))
    request=request_for(b['alice'])
    async def connected():return False
    request.is_disconnected=connected
    async def run():
        response=await endpoint(sid,request,0);it=response.body_iterator
        try:
            assert 'event: session' in await anext(it)
            assert 'FIRST_PRIVATE_TEXT' in await anext(it)
            app.state.store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
            frame=await anext(it)
            assert 'event: error' in frame and 'SECOND_PRIVATE_TEXT' not in frame
            with pytest.raises(StopAsyncIteration):await anext(it)
        finally:await it.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('concurrent',['login','provider_edit','user_epoch'])
def test_stale_userinfo_denial_does_not_disable_newer_identity(oidc,concurrent):
    app,b,fake,row,_=oidc;_,session=login(oidc);store=app.state.store
    identity=store.one('SELECT * FROM external_identities WHERE user_id=?',(session['user_id'],))
    fake.userinfo_subject='wrong-subject'
    def race():
        if concurrent=='login':
            store.execute('UPDATE external_identities SET upstream_tokens=? WHERE id=?',(store.encrypt('{}'),identity['id']))
        elif concurrent=='provider_edit':store.execute('UPDATE oidc_providers SET version=version+1 WHERE id=?',(row['id'],))
        else:store.execute('UPDATE iam_users SET epoch=epoch+1 WHERE user_id=?',(session['user_id'],))
    fake.on_userinfo=race
    with pytest.raises(DevError):asyncio.run(app.state.oidc.sync_identity(identity['id']))
    assert store.one('SELECT enabled FROM external_identities WHERE id=?',(identity['id'],))['enabled']==1


def test_authoritative_refresh_rejection_disables_current_identity(oidc):
    app,b,fake,_,_=oidc;_,session=login(oidc);store=app.state.store
    identity=store.one('SELECT * FROM external_identities WHERE user_id=?',(session['user_id'],))
    secured=json.loads(store.decrypt(identity['upstream_tokens']));secured['expires_at']=0
    store.execute('UPDATE external_identities SET upstream_tokens=? WHERE id=?',(store.encrypt(json.dumps(secured)),identity['id']))
    fake.fail_refresh=True
    with pytest.raises(DevError) as caught:asyncio.run(app.state.oidc.sync_identity(identity['id']))
    assert caught.value.code=='OIDC_CREDENTIAL_REJECTED'
    assert store.one('SELECT enabled FROM external_identities WHERE id=?',(identity['id'],))['enabled']==0


def test_pending_mcp_role_revoke_never_runs_after_device_reconnect(team):
    app,b=team;r=shared_role(app,b,['read','write','execute'])
    grant=must(credential(b['alice'],r,profile(b['alice'],r)))
    receipt=call(b['alice'],grant['token'],'shell_exec',{'project':'same-alias','command':'echo fixture','idempotency_key':'queued-offline-role'}).json()['result']['structuredContent']
    op=app.state.store.one('SELECT * FROM operations WHERE id=?',(receipt['operation_id'],))
    request=json.loads(app.state.store.decrypt(op['payload']))
    assert app.state.runtime.permission_error(op,request) is None
    app.state.store.execute('UPDATE role_assignments SET active=0 WHERE user_id=? AND role_id=?',('alice',r['id']))
    assert app.state.runtime.permission_error(op,request) is not None


def test_device_enrollment_and_connection_stop_after_owners_membership_removed(team):
    import re
    from types import SimpleNamespace
    app,b=team;store=app.state.store
    store.execute("UPDATE devices SET owner_user_id='alice' WHERE id='device-team'")
    body={'platform':'posix','hub_url':'https://fixture.invalid','allow_root':'/tmp/disposable'}
    ticket=must(b['alice'].post('/api/devices/device-team/install-ticket',json=body))
    match=re.search(r'--token\s+(rdi_[A-Za-z0-9_-]{43})',ticket['command'])
    assert match
    connection=SimpleNamespace(device_secret=store.one("SELECT secret FROM devices WHERE id='device-team'")['secret'])
    assert app.state.runtime.connection_authorized('device-team',connection)
    must(b['owner'].put('/api/iam/spaces/team/members/alice/suspension',json={'blocked':True}))
    assert not app.state.runtime.connection_authorized('device-team',connection)
    denied=b['alice'].post('/agent/enroll',headers={'Authorization':'Bearer '+match.group(1)})
    assert denied.status_code==401 and 'secret' not in denied.json()
    # Removal is a reversible policy pause, not silent deletion of the device.
    must(b['owner'].put('/api/iam/spaces/team/members/alice/suspension',json={'blocked':False}))
    assert app.state.runtime.connection_authorized('device-team',connection)
    accepted=b['alice'].post('/agent/enroll',headers={'Authorization':'Bearer '+match.group(1)})
    assert accepted.status_code==200
    record=store.one("SELECT space_id,owner_user_id FROM audit WHERE action='device.enrolled' ORDER BY id DESC LIMIT 1")
    assert record=={'space_id':'team','owner_user_id':'alice'}
