"""Integration boundaries absent from both upstream and the standalone Gateway.

Real Hub/SQL/IAM fixtures. No production authorization replacement and no network
credentials; the only backend double is the existing deterministic MCP transport.
"""
from __future__ import annotations
import asyncio
import json
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
import pytest
from hub import keyring
from hub.gateway.service import Gateway
from hub.store import Store
from shared.crypto import digest
from shared.core_contracts import CORE_TOOLS
from tests.test_iam_integration import team as team, shared_role, assign
from tests.test_mcp_gateway import gw as gw, configured, connector, account, publish
from tests.test_oidc_integration import oidc as oidc, start
from tests.test_oidc_review_regressions import seed_identity
from tests.test_roles import role, profile, credential, must, role_oauth
from tests.test_access_profiles import call, data


def operation(store, owner, *, space='team', visibility='private'):
    identifier=uuid.uuid4().hex
    store.execute('''INSERT INTO operations
        (id,project_id,device_id,actor,tool,args_summary,fingerprint,state,
         created,updated,space_id,owner_user_id,visibility)
        VALUES(?,?,?,?,'fs_read',?,'fixture','succeeded',1,1,?,?,?)''',
        (identifier,'project-'+space,'device-'+space,'panel:'+owner,
         json.dumps({'path':owner+'-PRIVATE'}),space,owner,visibility))
    return identifier


def test_call_log_watch_filters_detail_and_cursor_respect_private_space(team):
    app,b=team;shared_role(app,b,['read'])
    store=app.state.store
    own=[operation(store,'alice') for _ in range(2)]
    other=operation(store,'bob')
    cross=operation(store,'owner',space='legacy')
    shared=operation(store,'bob',visibility='space')
    result=must(b['alice'].get('/api/call-log',params={'limit':1,'watch':','.join([*own,other,cross,shared])}))
    visible=set(own+[shared])
    assert {r['id'] for r in result['updates']}==visible
    assert {r['id'] for r in result['operations']}<=visible
    assert cross not in json.dumps(result) and other not in json.dumps(result)
    assert {p['id'] for p in result['projects']}=={'project-team'}
    assert b['alice'].get('/api/call-log/'+other).status_code==404
    assert b['alice'].get('/api/call-log/'+cross).status_code==404
    assert b['bob'].get('/api/call-log',params={'limit':1,'cursor':result['next_cursor']}).status_code==400
    store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
    assert b['alice'].get('/api/call-log',params={'watch':own[0]}).status_code==403


@pytest.mark.parametrize('legacy',['fs_read','shell_exec','devices_list','projects_create'])
def test_removed_public_native_tools_never_fall_through_to_gateway(team,monkeypatch,legacy):
    app,b=team;r=shared_role(app,b,['read','write','execute'])
    p=profile(b['alice'],r);g=must(credential(b['alice'],r,p))
    async def no_fallback(*args,**kwargs):
        raise AssertionError('Unknown/removed native tool reached an external backend')
    monkeypatch.setattr(app.state.gateway,'call',no_fallback)
    # Deliberately bypass the migration helper: assert the actual public contract.
    response=b['alice'].post('/mcp',headers={'Authorization':'Bearer '+g['token'],'Accept':'application/json, text/event-stream'},json={
        'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':legacy,'arguments':{}}})
    payload=response.json()['result']
    assert payload['isError']
    assert json.loads(payload['content'][0]['text'])['error']['code']=='TOOL_REMOVED'


def test_native_catalog_is_nine_core_plus_stable_identity(team):
    app,b=team;r=shared_role(app,b);p=profile(b['alice'],r);g=must(credential(b['alice'],r,p))
    response=b['alice'].post('/mcp',headers={'Authorization':'Bearer '+g['token'],'Accept':'application/json, text/event-stream'},json={
        'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
    tools=response.json()['result']['tools']
    assert {t['name'] for t in tools}==CORE_TOOLS|{'get_profile','get_access_context'}
    assert next(t for t in tools if t['name']=='get_profile')['_meta']['openai/profile'] is True


def test_first_pat_consent_atomic_and_fixed_grant_cannot_opt_in(gw):
    app,b,backend=gw
    c=connector(b);a=account(b,c);binding=publish(b,a)
    r=role(b['owner'],project_rules=[],connector_rules=[{'binding_id':binding['id'],'tools':['echo']}])
    assign(b['owner'],r,'alice');p=profile(b['alice'],r)
    before=app.state.store.one('SELECT count(*) AS n FROM grants')['n']
    rejected=credential(b['alice'],r,p,confirm_external_mcp='yes')
    assert rejected.status_code==422
    assert app.state.store.one('SELECT count(*) AS n FROM grants')['n']==before
    g=must(credential(b['alice'],r,p,confirm_external_mcp=True))
    assert app.state.store.one('SELECT grant_id FROM gateway_consents WHERE grant_id=?',(g['grant_id'],))
    assert not call(b['alice'],g['token'],'kiln__echo',{'value':'allowed'}).json()['result']['isError']
    before=app.state.store.one('SELECT count(*) AS n FROM grants')['n']
    fixed=b['owner'].post('/api/grants',json={'label':'fixed','scopes':['read'],'projects':['project-team'],'confirm_external_mcp':True})
    assert fixed.status_code==403
    assert app.state.store.one('SELECT count(*) AS n FROM grants')['n']==before


def test_first_oauth_consent_includes_external_binding_without_second_grant(gw):
    app,b,backend=gw
    c=connector(b);a=account(b,c);binding=publish(b,a)
    r=role(b['owner'],project_rules=[],connector_rules=[{'binding_id':binding['id'],'tools':['echo']}])
    assign(b['owner'],r,'alice');p=profile(b['alice'],r)
    _,tokens=role_oauth(b['alice'],r,p,confirm_external_mcp=True)
    response=call(b['alice'],tokens['access_token'],'kiln__echo',{'value':'oauth'})
    assert not response.json()['result']['isError']
    row=app.state.store.one('SELECT grant_id FROM tokens WHERE hash=?',(digest(tokens['access_token']),))
    assert app.state.store.one('SELECT grant_id FROM gateway_consents WHERE grant_id=?',(row['grant_id'],))


def test_rotation_covers_gateway_oidc_and_preserves_retry_and_idempotency(gw,oidc,tmp_path):
    app,b,backend,c,a,binding,r,p,g=configured(gw)
    store=app.state.store
    call_result=call(b['alice'],g['token'],'kiln__echo',{'value':'saved'}).json()['result']
    call_id=call_result['_meta']['codepier/callId']
    provider=oidc[3]
    identity=seed_identity(app,provider)
    start(oidc)  # Encrypted pending PKCE verifier must survive rotation as well.
    store.execute('INSERT INTO oidc_sync_state(identity_id,attempted_at,next_attempt,failures,code,credential_hash,provider_version,observed_checked_at) VALUES(?,1,9999999999,2,?,?,?,?)',
                  (identity['id'],'OIDC_UPSTREAM_ERROR',digest(identity['upstream_tokens']),provider['version'],identity['checked_at']))
    expected={}
    for table,column in keyring.CIPHER_COLUMNS:
        primary='state_hash' if table=='oidc_transactions' else 'id'
        for row in store.all(f"SELECT {primary} AS id,{column} AS cipher FROM {table} WHERE {column} IS NOT NULL AND {column}!=''"):
            expected[(table,column,primary,row['id'])]=store.decrypt(row['cipher'])
    assert {'oidc_providers','oidc_transactions','external_identities','gateway_accounts','gateway_calls','gateway_secrets'}<={k[0] for k in expected}
    copied=tmp_path/'rotation-copy';copied.mkdir()
    with store.lock,sqlite3.connect(copied/'hub.sqlite3') as db:
        store.db.backup(db)
    shutil.copy2(store.directory/'master.key',copied/'master.key')
    result=keyring.rotate_key(copied)
    assert result['records']==len(expected)
    reopened=Store(copied)
    try:
        for (table,column,primary,identifier),plaintext in expected.items():
            row=reopened.one(f'SELECT {column} AS cipher FROM {table} WHERE {primary}=?',(identifier,))
            assert reopened.decrypt(row['cipher'])==plaintext
            assert row['cipher'].startswith('cp1:'+result['key_id']+':')
        rotated=reopened.one('SELECT upstream_tokens FROM external_identities WHERE id=?',(identity['id'],))['upstream_tokens']
        state=reopened.one('SELECT * FROM oidc_sync_state WHERE identity_id=?',(identity['id'],))
        assert state['credential_hash']==digest(rotated) and state['failures']==2
        gateway=Gateway(reopened)
        assert gateway.secret==app.state.gateway.secret
        saved=reopened.one('SELECT result FROM gateway_calls WHERE id=?',(call_id,))['result']
        assert json.loads(reopened.decrypt(saved))['structuredContent']['value']=='saved'
        asyncio.run(gateway.close())
    finally:reopened.close()


def test_oidc_entitlement_sql_runs_off_asgi_loop(oidc,monkeypatch):
    app,b,fake,provider,_=oidc
    from tests.test_oidc_review_regressions import run_sync
    seed_identity(app,provider,subject=fake.subject)
    original=app.state.oidc.reconcile_groups;seen=[]
    def record(*args):
        seen.append(threading.get_ident())
        assert app.state.store.lock.owned
        return original(*args)
    monkeypatch.setattr(app.state.oidc,'reconcile_groups',record)
    calling_thread=threading.get_ident();run_sync(app)
    assert seen and calling_thread not in seen


def test_user_suspension_sql_runs_in_worker_and_disconnects_after_commit(team, monkeypatch):
    app, browsers = team
    store = app.state.store
    original = store._security_write
    writes = []
    def observe(sql):
        if sql.startswith('UPDATE iam_users SET active='):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise AssertionError('User suspension blocks the ASGI event loop')
            writes.append(sql)
        return original(sql)
    monkeypatch.setattr(store.db, '_on_write', observe)
    store.execute("UPDATE devices SET owner_user_id='alice' WHERE id='device-team'")
    disconnected = []
    async def disconnect(identifier, reason):
        state = await store.run(store.one, 'SELECT active FROM iam_users WHERE user_id=?', ('alice',))
        assert state['active'] == 0
        disconnected.append(identifier)
    monkeypatch.setattr(app.state.runtime, 'disconnect_device', disconnect)
    must(browsers['owner'].put('/api/iam/users/alice', json={
        'active': False, 'instance_admin': False, 'expected_version': 1}))
    assert writes and disconnected == ['device-team']
