"""Connection previews use disposable HTTP/SQLite fixtures and never dispatch."""
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from hub.app import create_app
from shared.crypto import password_hash
from shared.role_contracts import ROLE_SCOPE


@pytest.fixture
def preview_api(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_PUBLIC_URL', 'http://testserver')
    monkeypatch.setenv('MCP_PUBLIC_URL', '')
    app = create_app(str(tmp_path / 'hub'))
    store = app.state.store
    sessions = {}
    with store.transaction():
        for uid in ('owner', 'bob'):
            store.db.execute('INSERT INTO users VALUES(?,?,?,?)',
                             (uid, uid, password_hash('disposable-preview-password'), time.time()))
            sessions[uid] = app.state.auth.new_session(uid)
        store.db.execute("INSERT INTO spaces(id,label,kind,created) VALUES('other','Other Space','team',?)", (time.time(),))
        store.db.execute("INSERT INTO memberships(space_id,user_id,level) VALUES('other','owner','owner')")
        for sid in ('legacy', 'other'):
            store.db.execute('INSERT INTO devices(id,name,secret,space_id,owner_user_id,created) VALUES(?,?,?,?,?,?)',
                             ('device-' + sid, 'Node', store.encrypt('fixture-device-secret'), sid, 'owner', time.time()))
        for pid, sid in [('p1', 'legacy'), ('p2', 'legacy'), ('secret-other-project', 'other')]:
            store.db.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,space_id,owner_user_id,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (pid, pid, pid, 'device-' + sid, str(tmp_path / pid), 'write', 1, sid, 'owner', time.time()))
    with TestClient(app) as client:
        client.headers.update({'Cookie': 'rd_session=' + sessions['owner']['cookie'],
                               'X-RD-CSRF': sessions['owner']['csrf'], 'X-CodePier-Space': 'legacy'})
        yield app, client, sessions


def _ok(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def _grant(client, **values):
    return _ok(client.post('/api/grants', json={
        'label': 'Preview connection', 'scopes': ['read', 'write', 'execute', 'computer'],
        'projects': ['p1', 'p2'], **values,
    }))


def _profile(client, **values):
    return _ok(client.post('/api/access-profiles', json={
        'label': 'Preview profile', 'scopes': ['read', 'write', 'execute', 'computer'],
        'projects': ['p1', 'p2'], 'idempotency_key': uuid.uuid4().hex, **values,
    }), 201)


def _role(client, **values):
    return _ok(client.post('/api/access-roles', json={
        'label': 'Preview role',
        'project_rules': [{'projects': ['p1'], 'actions': ['read', 'execute']},
                          {'projects': ['p2'], 'actions': ['read', 'write']}],
        'idempotency_key': uuid.uuid4().hex, **values,
    }), 201)


def _dynamic(client):
    role = _role(client)
    profile = _profile(client, role_id=role['id'])
    grant = _grant(client, scopes=[ROLE_SCOPE], projects=[], authorization_mode='role',
                   profile_id=profile['id'], profile_version=profile['version'],
                   role_version=role['version'], confirm_dynamic_role=True)
    return grant, profile, role


def _preview(client, grant):
    return _ok(client.get('/api/grants/' + grant['grant_id'] + '/access-preview'))


def _actions(value, project='p1'):
    return next(row['actions'] for row in value['projects'] if row['id'] == project)


def _denied(value):
    assert all(not action['allowed'] for row in value['projects'] for action in row['actions'].values())


def test_preview_contract_read_only_no_secrets_and_no_dispatch(preview_api, monkeypatch):
    app, client, _ = preview_api
    grant = _grant(client)
    def forbidden(*args, **kwargs):
        pytest.fail('A read-only permission preview must not invoke/dispatch/audit an operation')
    for name in ('invoke', '_invoke', 'dispatch', 'authorize'):
        monkeypatch.setattr(app.state.runtime, name, forbidden)
    with app.state.store.lock:
        before = app.state.store.db.total_changes
    value = _preview(client, grant)
    with app.state.store.lock:
        assert app.state.store.db.total_changes == before
    assert value['owner'] == {'id': 'owner', 'label': 'owner'}
    assert value['space']['id'] == 'legacy'
    assert value['grant']['mode'] == 'fixed' and value['grant']['state'] == 'active'
    assert value['grant']['expires_at'] == grant['expires']
    assert value['evaluation'] == 'current_hub_policy'
    assert value['runtime_checks']['status'] == 'not_verified'
    assert value['profile'] is None and value['role'] is None
    assert all(action['allowed'] for action in _actions(value).values())
    assert {p['id'] for p in value['projects']} == {'p1', 'p2'}
    serialized = json.dumps(value)
    token = app.state.store.one('SELECT id,hash FROM tokens WHERE grant_id=?', (grant['grant_id'],))
    for secret in (grant['token'], token['id'], token['hash'], 'fixture-device-secret', 'secret-other-project', str(app.state.store.directory)):
        assert secret not in serialized
    assert app.state.store.one('SELECT count(*) AS n FROM operations')['n'] == 0


def test_preview_requires_current_owner_and_space(preview_api):
    _, client, sessions = preview_api
    grant = _grant(client)
    path = '/api/grants/' + grant['grant_id'] + '/access-preview'
    assert client.get(path, headers={'Cookie': 'rd_session=' + sessions['bob']['cookie']}).status_code == 404
    assert client.get(path, headers={'X-CodePier-Space': 'other'}).status_code == 404
    assert client.get(path, headers={'Cookie': ''}).status_code == 401
    assert client.get(path, headers={'X-CodePier-Space': 'missing'}).status_code == 404


def test_fixed_profile_narrowing_and_reexpansion_never_exceed_consent(preview_api):
    app, client, _ = preview_api
    profile = _profile(client)
    grant = _grant(client, profile_id=profile['id'], profile_version=1, scopes=['read', 'write'], projects=['p1'])
    value = _preview(client, grant)
    assert value['grant']['mode'] == 'fixedProfile'
    assert value['profile']['id'] == profile['id']
    assert _actions(value)['write']['allowed']
    assert not _actions(value)['execute']['allowed']
    assert not _actions(value, 'p2')['read']['allowed']
    app.state.store.execute('UPDATE access_profiles SET scopes=? WHERE id=?', (json.dumps(['read']), profile['id']))
    assert not _actions(_preview(client, grant))['write']['allowed']
    app.state.store.execute('UPDATE access_profiles SET scopes=? WHERE id=?', (json.dumps(['read', 'write', 'execute', 'computer']), profile['id']))
    value = _preview(client, grant)
    assert _actions(value)['write']['allowed']
    assert not _actions(value)['execute']['allowed']
    assert not _actions(value, 'p2')['read']['allowed']
    app.state.store.execute('UPDATE access_profiles SET projects=? WHERE id=?', (json.dumps(['p2']), profile['id']))
    _denied(_preview(client, grant))


def test_dynamic_rule_actions_stay_paired_with_resources_and_update_live(preview_api):
    app, client, _ = preview_api
    grant, _, role = _dynamic(client)
    value = _preview(client, grant)
    assert value['grant']['mode'] == 'role'
    assert _actions(value)['execute']['allowed'] and not _actions(value)['write']['allowed']
    assert _actions(value, 'p2')['write']['allowed'] and not _actions(value, 'p2')['execute']['allowed']
    policy = {'project_rules': [{'projects': ['p2'], 'actions': ['read', 'execute']}], 'device_rules': []}
    app.state.store.execute('UPDATE access_roles SET policy=?,version=version+1 WHERE id=?', (json.dumps(policy), role['id']))
    value = _preview(client, grant)
    assert not _actions(value)['read']['allowed'] and _actions(value, 'p2')['execute']['allowed']
    assert value['role']['version'] == 2
    app.state.store.execute('UPDATE access_roles SET enabled=0 WHERE id=?', (role['id'],))
    value = _preview(client, grant)
    assert value['grant']['state'] == 'paused'
    _denied(value)


def test_profile_role_binding_does_not_upgrade_old_fixed_consent(preview_api):
    app, client, _ = preview_api
    role = _role(client)
    profile = _profile(client)
    grant = _grant(client, scopes=['read'], projects=['p1'], profile_id=profile['id'], profile_version=1)
    app.state.store.execute('UPDATE access_profiles SET role_id=? WHERE id=?', (role['id'], profile['id']))
    value = _preview(client, grant)
    assert value['grant']['mode'] == 'fixedProfile'
    assert _actions(value)['read']['allowed']
    assert not _actions(value)['execute']['allowed']
    assert not _actions(value, 'p2')['read']['allowed']


@pytest.mark.parametrize('change,state,code', [
    ('revoked', 'revoked', 'GRANT_REVOKED'),
    ('expired', 'expired', 'TOKEN_EXPIRED'),
    ('missing', 'unavailable', 'CREDENTIAL_UNAVAILABLE'),
    ('refresh', 'refresh_required', 'TOKEN_REFRESH_REQUIRED'),
    ('pending', 'pending', 'CONNECTION_PENDING'),
    ('epoch', 'blocked', 'INVALID_TOKEN'),
    ('corrupt', 'blocked', 'INVALID_TOKEN'),
    ('resource', 'blocked', 'INVALID_TOKEN'),
])
def test_connection_lifecycle_is_not_a_false_allow(preview_api, change, state, code):
    app, client, _ = preview_api
    store = app.state.store
    grant = _grant(client)
    gid = grant['grant_id']
    if change == 'revoked':
        store.execute('UPDATE grants SET revoked=1 WHERE id=?', (gid,))
    elif change == 'expired':
        store.execute('UPDATE tokens SET expires=? WHERE grant_id=?', (time.time() - 1, gid))
    elif change in {'missing', 'pending'}:
        store.execute('DELETE FROM tokens WHERE grant_id=?', (gid,))
        if change == 'pending':
            store.execute("INSERT INTO oauth_codes VALUES('fixture-code-hash','fixture-client','http://localhost/callback','challenge','http://testserver/mcp',?,?)", (gid, time.time() + 100))
    elif change == 'refresh':
        store.execute("UPDATE tokens SET kind='refresh' WHERE grant_id=?", (gid,))
    elif change == 'epoch':
        store.execute('UPDATE grants SET user_epoch=user_epoch+1 WHERE id=?', (gid,))
    elif change == 'corrupt':
        store.execute("UPDATE grants SET scopes='broken-json' WHERE id=?", (gid,))
    elif change == 'resource':
        store.execute("UPDATE tokens SET kind='access' WHERE grant_id=?", (gid,))
        store.execute("UPDATE grants SET resource='http://old-resource/mcp' WHERE id=?", (gid,))
    value = _preview(client, grant)
    assert value['grant']['state'] == state
    assert value['grant']['reason']['code'] == code
    _denied(value)


def test_disabled_profile_blocks_current_preview(preview_api):
    app, client, _ = preview_api
    profile = _profile(client)
    grant = _grant(client, profile_id=profile['id'], profile_version=1)
    app.state.store.execute('UPDATE access_profiles SET enabled=0 WHERE id=?', (profile['id'],))
    value = _preview(client, grant)
    assert value['grant']['state'] == 'blocked' and not value['profile']['enabled']
    _denied(value)


def test_hub_project_flags_are_checked_instead_of_scopes_only(preview_api):
    app, client, _ = preview_api
    grant = _grant(client)
    app.state.store.execute("UPDATE projects SET mode='read' WHERE id='p1'")
    actions = _actions(_preview(client, grant))
    assert actions['read']['allowed']
    for action in ('write', 'execute', 'computer'):
        assert not actions[action]['allowed'] and actions[action]['reason']['code'] == 'READ_ONLY'
    app.state.store.execute("UPDATE projects SET mode='write',allow_tasks=0 WHERE id='p1'")
    actions = _actions(_preview(client, grant))
    assert actions['write']['allowed'] and actions['computer']['allowed']
    assert not actions['execute']['allowed'] and actions['execute']['reason']['code'] == 'TASKS_DISABLED'


def test_fixed_grant_respects_live_iam_and_hidden_projects(preview_api):
    app, client, sessions = preview_api
    role = _role(client, project_rules=[{'projects': ['p1'], 'actions': ['read']}])
    grant = _grant(client)
    store = app.state.store
    with store.transaction():
        store.db.execute("UPDATE memberships SET level='member' WHERE user_id='owner' AND space_id='legacy'")
        store.db.execute('INSERT INTO role_assignments(space_id,user_id,role_id,active,may_delegate) VALUES(?,?,?,1,1)', ('legacy', 'owner', role['id']))
    value = _preview(client, grant)
    assert {p['id'] for p in value['projects']} == {'p1'}
    assert _actions(value)['read']['allowed']
    assert not _actions(value)['write']['allowed']
    assert _actions(value)['write']['reason']['code'] == 'ROLE_POLICY_DENIED'
    store.execute("INSERT INTO membership_blocks(space_id,user_id,blocked) VALUES('legacy','owner',1)")
    assert client.get('/api/grants/' + grant['grant_id'] + '/access-preview').status_code == 403
