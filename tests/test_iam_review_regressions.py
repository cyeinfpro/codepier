"""PR review regressions against the actual IAM/runtime and SQL persistence."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest

from hub import iam
from hub.iam_read import read_decision
from hub.runtime import Principal
from hub.store import Store
from shared.util import DevError
from tests.test_iam_integration import team as team, shared_role
from tests.test_roles import role, must


def principal(user='alice',space='team'):
    return Principal('panel:'+user,user,{'read'},[],space_id=space)


def projects(store,count):
    with store.transaction():
        for n in range(count):
            store.db.execute('''INSERT OR IGNORE INTO projects
                (id,alias,alias_key,device_id,root,mode,space_id,owner_user_id,created)
                VALUES(?,?,?,?,?,'write','team','owner',?)''',
                (f'load-{n}',f'load-{n}',f'load-{n}','device-team',f'/tmp/load-{n}',time.time()))


def queries(store,call):
    statements=[]
    store.db.set_trace_callback(statements.append)
    try:result=call()
    finally:store.db.set_trace_callback(None)
    return result,[x for x in statements if x.lstrip().upper().startswith(('SELECT','WITH'))]


def test_live_principal_query_count_does_not_scale_with_projects_or_roles(team,record_property):
    app,b=team;store=app.state.store;shared_role(app,b,['read','write'])
    for n in range(3):
        r=role(b['owner'],label=f'Additional {n}',project_rules=[{'actions':['read'],'all_projects':True}])
        must(b['owner'].put('/api/iam/spaces/team/assignments/'+r['id']+'/alice',json={'active':True,'may_delegate':True,'expected_version':0}))
    projects(store,10)
    small,small_sql=queries(store,lambda:iam.live_principal(store,principal()))
    projects(store,250)
    large,large_sql=queries(store,lambda:iam.live_principal(store,principal()))
    record_property("selects_11_projects_4_roles",len(small_sql))
    record_property("selects_251_projects_4_roles",len(large_sql))
    assert len(small.projects)==11 and len(large.projects)==251
    assert len(large_sql)<=len(small_sql)+1 and len(large_sql)<=10
    assert 'write' in large.scopes
    assert sum('FROM role_created_projects' in sql for sql in large_sql)==1
    assert sum('JOIN role_assignments' in sql for sql in large_sql)==1


def test_actual_device_endpoint_query_count_not_multiplied_by_project_count(team,record_property):
    app,b=team;store=app.state.store;shared_role(app,b)
    store.execute("UPDATE devices SET owner_user_id='alice' WHERE id='device-team'")
    projects(store,10)
    small,small_sql=queries(store,lambda:b['alice'].get('/api/devices'))
    projects(store,250)
    large,large_sql=queries(store,lambda:b['alice'].get('/api/devices'))
    record_property("device_endpoint_selects_11_projects",len(small_sql))
    record_property("device_endpoint_selects_251_projects",len(large_sql))
    assert small.status_code==large.status_code==200
    assert len(large_sql)<=len(small_sql)+2
    assert len(large_sql)<45


def operation(store,identifier='review-op'):
    store.execute('''INSERT INTO operations
        (id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated,space_id,owner_user_id)
        VALUES(?,'device-team','project-team','panel:alice','fs_read','{}','','succeeded',?,?,'team','alice')''',
        (identifier,time.time(),time.time()))


def test_operation_read_reuses_one_principal_snapshot_and_reloads_next_call(team):
    app,b=team;store=app.state.store;shared_role(app,b);operation(store);projects(store,200)
    row,sql=queries(store,lambda:app.state.runtime.operation_row('review-op',principal()))
    assert row['id']=='review-op'
    assert sum('SELECT id FROM projects WHERE space_id=' in q for q in sql)==1
    assert sum('JOIN role_assignments' in q for q in sql)==1
    store.execute("UPDATE role_assignments SET active=0 WHERE user_id='alice'")
    with pytest.raises(DevError):app.state.runtime.operation_row('review-op',principal())


@pytest.mark.parametrize('field',['active','may_delegate'])
def test_role_or_delegation_revocation_is_not_a_cached_session_permission(team,field):
    from tests.test_roles import profile,credential
    from tests.test_access_profiles import call,data
    app,b=team;r=shared_role(app,b)
    g=must(credential(b['alice'],r,profile(b['alice'],r)))
    assert data(call(b['alice'],g['token'],'projects_list'))
    app.state.store.execute(f'UPDATE role_assignments SET {field}=0 WHERE role_id=? AND user_id=?',(r['id'],'alice'))
    assert call(b['alice'],g['token'],'projects_list').status_code in (401,403)


def test_scope_invalidates_on_in_scope_policy_write(team):
    app,b=team;shared_role(app,b);store=app.state.store;p=principal()
    with iam.read_scope(store):
        assert iam.live_principal(store,p).projects
        store.execute("UPDATE role_assignments SET active=0 WHERE user_id='alice'")
        assert not iam.live_principal(store,p).projects
    assert not iam.live_principal(store,p).projects


def test_snapshot_is_not_inherited_as_authority_by_scheduled_task(team):
    app,b=team;shared_role(app,b);store=app.state.store;p=principal()
    async def later():return iam.live_principal(store,p)
    @read_decision
    def schedule(store):
        assert iam.live_principal(store,p).projects
        return asyncio.create_task(later())
    async def run():
        task=schedule(store)
        store.execute("UPDATE role_assignments SET active=0 WHERE user_id='alice'")
        result=await task
        assert not result.projects
    asyncio.run(run())


@pytest.mark.parametrize('kind',['async','generator','async_generator'])
def test_read_scope_cannot_decorate_a_wait_or_stream(kind):
    async def coroutine(store):return None
    def generator(store):yield None
    async def async_generator(store):yield None
    function={'async':coroutine,'generator':generator,'async_generator':async_generator}[kind]
    with pytest.raises(TypeError):read_decision(function)


def test_each_stream_event_observes_current_policy(team):
    app,b=team;shared_role(app,b);store=app.state.store;operation(store)
    event={'type':'operation','data':{'id':'review-op'}}
    assert iam.event_visible(app.state.runtime,principal(),event)
    store.execute("UPDATE role_assignments SET active=0 WHERE user_id='alice'")
    assert not iam.event_visible(app.state.runtime,principal(),event)


def test_action_resource_pairs_and_created_project_rules_survive_batching(team):
    app,b=team;store=app.state.store;projects(store,3)
    r=role(b['owner'],label='Precise',project_rules=[{'actions':['read'],'all_projects':True},
        {'actions':['read','execute'],'projects':['load-0']},
        {'actions':['read','write'],'created_projects':True,'excluded_projects':['load-2']}])
    must(b['owner'].put('/api/iam/spaces/team/assignments/'+r['id']+'/alice',json={'active':True,'may_delegate':True,'expected_version':0}))
    for pid in ('load-1','load-2'):
        store.execute('INSERT INTO role_created_projects VALUES(?,?,?)',(r['id'],pid,time.time()))
    with iam.read_scope(store):
        p=iam.live_principal(store,principal())
        assert iam.human_project_actions(store,p,'load-0')=={'read','execute'}
        assert iam.human_project_actions(store,p,'load-1')=={'read','write'}
        assert iam.human_project_actions(store,p,'load-2')=={'read'}
        assert iam.human_project_actions(store,p,'project-legacy')==set()


@pytest.mark.parametrize('target',['project-team','review-op','device-team'])
def test_freeform_failed_login_target_cannot_select_a_space(team,target):
    app,b=team;operation(app.state.store)
    response=b['bob'].post('/api/login',json={'username':target,'password':'not-a-real-password'})
    assert response.status_code==401
    event=app.state.store.one("SELECT * FROM audit WHERE action='auth.login' AND actor='anonymous' ORDER BY id DESC LIMIT 1")
    assert event['target']==target and event['space_id']=='legacy' and event['owner_user_id'] is None
    team_log=b['owner'].get('/api/audit').json()['events']
    assert not any(row['id']==event['id'] for row in team_log)


def test_audit_literal_target_has_no_resource_lookup(team):
    app,_=team;store=app.state.store
    token=iam.audit_context.set(None)
    try:
        _,sql=queries(store,lambda:store.audit('anonymous','auth.login','project-team','failed'))
        assert not sql
        _,sql=queries(store,lambda:store.audit('hub','project.inspected','project-team',target_kind='project'))
        assert len(sql)==1 and 'FROM projects' in sql[0]
        assert store.one('SELECT space_id FROM audit ORDER BY id DESC LIMIT 1')['space_id']=='team'
    finally:iam.audit_context.reset(token)


def test_typed_target_never_overrides_scoped_actor(team):
    app,_=team;store=app.state.store
    token=iam.audit_context.set((store,'panel:alice','team','alice'))
    try:
        _,sql=queries(store,lambda:store.audit('panel:alice','read.denied','project-legacy','denied',target_kind='project'))
        row=store.one('SELECT * FROM audit ORDER BY id DESC LIMIT 1')
        assert not sql and row['space_id']=='team' and row['owner_user_id']=='alice'
    finally:iam.audit_context.reset(token)


@pytest.mark.parametrize('kind',['projects','users','unknown',"projects; DROP TABLE users"])
def test_audit_target_kind_is_an_internal_allowlist(team,kind):
    with pytest.raises(ValueError):team[0].state.store.audit('hub','inspection','id',target_kind=kind)


@pytest.mark.parametrize('target',['project-team','project-legacy','nonexistent'])
def test_fixed_grant_edit_denies_member_before_project_lookup(team,target):
    app,b=team;store=app.state.store
    store.execute('''INSERT INTO grants(id,user_id,label,scopes,projects,revoked,created,space_id,owner_user_id)
                     VALUES('legacy-fixed','alice','fixed','["read"]','[]',0,?,'team','alice')''',(time.time(),))
    response=b['alice'].put('/api/grants/legacy-fixed/projects',json={'projects':[target],'all_projects':False,'expected_projects':[],'expected_revision':0})
    assert response.status_code==403 and response.json()['error']['code']=='ROLE_REQUIRED'
    assert json.loads(store.one("SELECT projects FROM grants WHERE id='legacy-fixed'")['projects'])==[]


def test_schema_nine_upgrade_preserves_records_and_adds_only_resilience_state(tmp_path):
    folder=tmp_path/'migration';store=Store(folder)
    from shared.crypto import password_hash
    store.execute('INSERT INTO users VALUES(?,?,?,?)',('owner','owner',password_hash('fixture-password'),time.time()))
    key=(folder/'master.key').read_bytes()
    user=store.one('SELECT * FROM users');members=store.all('SELECT * FROM memberships')
    # Reconstruct the pre-review schema 9: retry state/client buckets did not exist.
    with store.transaction():
        store.db.execute('DROP TABLE oidc_sync_audit');store.db.execute('DROP TABLE oidc_sync_state')
        store.db.execute('DROP INDEX oidc_login_client');store.db.execute('DROP INDEX oidc_login_provider')
        store.db.execute('ALTER TABLE oidc_transactions DROP COLUMN client_hash')
        store.db.execute("UPDATE meta SET value='9' WHERE key='schema'")
    store.close()
    reopened=Store(folder)
    try:
        assert reopened.one("SELECT value FROM meta WHERE key='schema'")['value']=='10'
        assert reopened.one('SELECT * FROM users')==user
        assert reopened.all('SELECT * FROM memberships')==members
        assert (folder/'master.key').read_bytes()==key
        assert not reopened.all('SELECT * FROM oidc_sync_state')
        assert not reopened.all('PRAGMA foreign_key_check')
    finally:reopened.close()
