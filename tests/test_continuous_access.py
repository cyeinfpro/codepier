"""Real OAuth/PAT requests for durable current-and-future project authorization."""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from shared.crypto import digest
from tests.test_audit_api import api, oauth_tokens, rpc


def add_project(app, identifier='future'):
    app.state.store.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)',
                            (identifier, identifier, identifier, 'device', '/tmp/' + identifier,
                             '', 'write', 0, time.time()))


def visible_projects(client, credential):
    response = rpc(client, credential, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                       'params': {'name': 'workspace', 'arguments': {'operation': 'list'}}})
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert not result.get('isError'), result
    value = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    data = value.get('data', value)
    return {project['id'] for project in data['projects']}


def grant_for(app, credential):
    return dict(app.state.store.one('SELECT g.* FROM grants g JOIN tokens t ON t.grant_id=g.id WHERE t.hash=?',
                                    (digest(credential),)))


def update_range(client, grant_id, projects=None, all_projects=False, snapshot=None):
    if snapshot is None:
        read = client.get('/api/grants/' + grant_id + '/projects')
        assert read.status_code == 200, read.text
        snapshot = read.json()
    body = {'projects': projects or [], 'all_projects': all_projects,
            'expected_projects': snapshot['projects'], 'expected_revision': snapshot['project_revision']}
    return client.put('/api/grants/' + grant_id + '/projects', json=body)


def authorize_request(client, scopes='read write execute'):
    registration = client.post('/oauth/register', json={'redirect_uris': ['http://localhost:12345/callback']}).json()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(('v' * 64).encode()).digest()).rstrip(b'=').decode()
    response = client.get('/oauth/authorize', params={'response_type': 'code', 'client_id': registration['client_id'],
        'redirect_uri': 'http://localhost:12345/callback', 'code_challenge_method': 'S256',
        'code_challenge': challenge, 'resource': 'http://testserver/mcp', 'scope': scopes}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return parse_qs(urlsplit(response.headers['location']).query)['authorize'][0]


def test_defaults_are_opt_in_and_never_auto_consent_or_expand_existing_grants(api):
    app, client, pat = api
    _, tokens = oauth_tokens(client)
    before = grant_for(app, tokens['access_token'])
    assert client.get('/api/settings').json()['access_defaults'] == {'all_projects': False, 'developer_scopes': False}
    response = client.put('/api/settings/access', json={'all_projects': True, 'developer_scopes': True})
    assert response.status_code == 200, response.text
    assert response.json()['updated_grants'] == 0
    assert grant_for(app, tokens['access_token']) == before
    request_id = authorize_request(client, 'read')
    count = app.state.store.one('SELECT count(*) AS n FROM grants')['n']
    details = client.get('/api/oauth/requests/' + request_id).json()
    assert details['access_defaults'] == {'all_projects': True, 'developer_scopes': True}
    assert details['scopes'] == ['read']
    assert app.state.store.one('SELECT count(*) AS n FROM grants')['n'] == count
    endpoint = '/api/oauth/requests/' + request_id + '/decide'
    assert client.post(endpoint, json={'allow': True, 'scopes': ['read'], 'projects': []}).status_code == 400
    assert client.post(endpoint, json={'allow': True, 'scopes': ['read', 'execute'], 'all_projects': True}).status_code == 400
    assert client.post(endpoint, json={'allow': True, 'scopes': ['read'], 'all_projects': True}).status_code == 200
    newest = app.state.store.one('SELECT scopes,projects FROM grants ORDER BY created DESC LIMIT 1')
    assert json.loads(newest['projects']) == ['*'] and json.loads(newest['scopes']) == ['read']
    assert visible_projects(client, pat) == {'project'}


def test_existing_oauth_token_sees_future_projects_without_reconnect_and_can_shrink(api):
    app, client, pat = api
    client_id, tokens = oauth_tokens(client)
    old_grant = grant_for(app, tokens['access_token'])
    before_tokens = [dict(row) for row in app.state.store.all('SELECT * FROM tokens ORDER BY id')]
    response = client.put('/api/settings/access', json={
        'all_projects': True, 'developer_scopes': True, 'apply_to_existing': True})
    assert response.status_code == 200, response.text
    assert response.json()['updated_grants'] == 1
    updated = grant_for(app, tokens['access_token'])
    assert {k: v for k, v in updated.items() if k != 'projects'} == {k: v for k, v in old_grant.items() if k != 'projects'}
    assert [dict(row) for row in app.state.store.all('SELECT * FROM tokens ORDER BY id')] == before_tokens
    add_project(app)
    assert visible_projects(client, tokens['access_token']) == {'project', 'future'}
    assert visible_projects(client, pat) == {'project'}
    # Disabling future consent defaults does not silently alter existing grants.
    assert client.put('/api/settings/access', json={'all_projects': False}).status_code == 200
    assert visible_projects(client, tokens['access_token']) == {'project', 'future'}
    refreshed = client.post('/oauth/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
                                                 'refresh_token': tokens['refresh_token']})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()['scope'] == 'read'
    assert visible_projects(client, refreshed.json()['access_token']) == {'project', 'future'}
    assert update_range(client, old_grant['id'], projects=['project']).status_code == 200
    assert visible_projects(client, tokens['access_token']) == {'project'}
    assert visible_projects(client, refreshed.json()['access_token']) == {'project'}
    assert client.delete('/api/grants/' + old_grant['id']).status_code == 200
    assert rpc(client, tokens['access_token'], {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'}).status_code == 401


def test_pat_wildcard_requires_explicit_option_and_survives_empty_project_list(api):
    app, client, _ = api
    app.state.store.execute('DELETE FROM projects')
    body = {'label': 'future PAT', 'scopes': ['read'], 'all_projects': True}
    response = client.post('/api/grants', json=body)
    assert response.status_code == 200, response.text
    credential = response.json()['token']
    assert visible_projects(client, credential) == set()
    add_project(app)
    assert visible_projects(client, credential) == {'future'}
    assert client.post('/api/grants', json={**body, 'projects': ['*']}).status_code == 400
    assert client.post('/api/grants', json={**body, 'all_projects': False}).status_code == 400


@pytest.mark.parametrize('invalid', ['true', 1, 0, None, [], {}])
def test_all_projects_is_strict_for_settings_pat_and_oauth(api, invalid):
    _, client, _ = api
    assert client.put('/api/settings/access', json={'all_projects': invalid}).status_code == 422
    assert client.post('/api/grants', json={'label': 'bad', 'scopes': ['read'], 'projects': ['project'],
                                          'all_projects': invalid}).status_code == 422
    request_id = authorize_request(client)
    response = client.post('/api/oauth/requests/' + request_id + '/decide', json={
        'allow': True, 'scopes': ['read'], 'projects': ['project'], 'all_projects': invalid})
    assert response.status_code == 400


def test_range_edit_detects_stale_dialog_and_aba_but_same_success_is_idempotent(api):
    app, client, pat = api
    grant = grant_for(app, pat)
    initial = client.get('/api/grants/' + grant['id'] + '/projects').json()
    widened = update_range(client, grant['id'], all_projects=True, snapshot=initial)
    assert widened.status_code == 200, widened.text
    assert widened.json()['project_revision'] == 1
    assert update_range(client, grant['id'], all_projects=True, snapshot=initial).status_code == 200
    assert update_range(client, grant['id'], projects=['project']).status_code == 200
    # Returning to the original project set must not make an old widening request current again.
    stale = update_range(client, grant['id'], all_projects=True, snapshot=initial)
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'GRANT_CHANGED'
    assert visible_projects(client, pat) == {'project'}
    app.state.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (grant['id'],))
    revoked = update_range(client, grant['id'], projects=['project'])
    assert revoked.status_code == 409 and revoked.json()['error']['code'] == 'GRANT_REVOKED'


def test_bulk_apply_ignores_pat_revoked_expired_and_other_owner_grants(api):
    app, client, pat = api
    store = app.state.store
    credentials = [oauth_tokens(client)[1]['access_token'] for _ in range(4)]
    grants = [grant_for(app, value) for value in credentials]
    store.execute('UPDATE grants SET revoked=1 WHERE id=?', (grants[1]['id'],))
    store.execute('UPDATE tokens SET expires=? WHERE grant_id=?', (time.time() - 1, grants[2]['id']))
    store.execute('INSERT INTO users VALUES (?,?,?,?)', ('other', 'other', 'unused', time.time()))
    store.execute('UPDATE grants SET user_id=? WHERE id=?', ('other', grants[3]['id']))
    response = client.put('/api/settings/access', json={'all_projects': True, 'apply_to_existing': True})
    assert response.status_code == 200 and response.json()['updated_grants'] == 1, response.text
    assert json.loads(grant_for(app, credentials[0])['projects']) == ['*']
    for value in [pat, *credentials[1:]]:
        assert json.loads(grant_for(app, value)['projects']) == ['project']
    assert client.get('/api/grants/' + grants[3]['id'] + '/projects').status_code == 404
    response = update_range(client, grants[3]['id'], all_projects=True,
                            snapshot={'projects': ['project'], 'project_revision': 0})
    assert response.status_code == 404


def test_access_changes_require_session_csrf_and_reject_foreign_origin(api):
    app, client, pat = api
    gid = grant_for(app, pat)['id']
    cases = [('/api/settings/access', {'all_projects': True}),
             ('/api/grants/' + gid + '/projects', {'all_projects': True, 'projects': [],
                                                  'expected_projects': ['project'], 'expected_revision': 0})]
    for path, body in cases:
        assert client.put(path, json=body, headers={'X-RD-CSRF': 'wrong'}).status_code == 403
        assert client.put(path, json=body, headers={'Origin': 'https://evil.invalid'}).status_code == 403
    client.cookies.clear()
    for path, body in cases:
        assert client.put(path, json=body, headers={'Authorization': 'Bearer ' + pat}).status_code == 401
    assert json.loads(grant_for(app, pat)['projects']) == ['project']


def test_bulk_failure_rolls_back_defaults_and_project_ranges(api):
    app, client, _ = api
    _, tokens = oauth_tokens(client)
    store = app.state.store
    before = grant_for(app, tokens['access_token'])
    store.execute("CREATE TRIGGER reject_project_update BEFORE UPDATE OF projects ON grants BEGIN SELECT RAISE(ABORT, 'injected scope write failure'); END")
    response = client.put('/api/settings/access', json={'all_projects': True, 'apply_to_existing': True})
    # Production converts SQLite integrity errors into a non-secret conflict response.
    assert response.status_code == 409 and response.json()['error']['code'] == 'CONFLICT'
    assert grant_for(app, tokens['access_token']) == before
    assert client.get('/api/settings').json()['access_defaults'] == {'all_projects': False, 'developer_scopes': False}


def test_invalid_defaults_fail_closed_and_partial_public_url_update_preserves_defaults(api):
    app, client, _ = api
    store = app.state.store
    store.execute('INSERT INTO meta(key,value) VALUES (?,?)', ('mcp_access_defaults:owner', '[true]'))
    assert client.get('/api/settings').json()['access_defaults'] == {'all_projects': False, 'developer_scopes': False}
    assert client.put('/api/settings/access', json={'all_projects': True, 'developer_scopes': True}).status_code == 200
    assert client.put('/api/settings', json={'public_url': 'http://testserver'}).status_code == 200
    assert client.get('/api/settings').json()['access_defaults'] == {'all_projects': True, 'developer_scopes': True}
    rejected = client.put('/api/settings/access', json={'all_projects': False, 'apply_to_existing': True})
    assert rejected.status_code == 400
    assert client.get('/api/settings').json()['access_defaults']['all_projects'] is True


def test_grant_list_status_survives_expired_token_cleanup(api):
    app, client, _ = api
    store = app.state.store
    grants = []
    for label in ['active', 'expired', 'cleaned', 'used-refresh', 'pending', 'revoked']:
        response = client.post('/api/grants', json={'label': label, 'scopes': ['read'], 'projects': ['project']})
        assert response.status_code == 200, response.text
        grants.append(response.json()['grant_id'])
    active, expired, cleaned, used, pending, revoked = grants
    store.execute('UPDATE tokens SET expires=? WHERE grant_id=?', (time.time() - 10, expired))
    for identifier in [cleaned, pending]:
        store.execute('DELETE FROM tokens WHERE grant_id=?', (identifier,))
    store.execute("UPDATE tokens SET kind='refresh_used' WHERE grant_id=?", (used,))
    store.execute('INSERT INTO oauth_codes VALUES (?,?,?,?,?,?,?)',
                  ('fixture-pending-code', 'fixture-client', 'http://localhost/callback', 'challenge',
                   'http://testserver/mcp', pending, time.time() + 300))
    assert client.delete('/api/grants/' + revoked).status_code == 200
    rows = {row['id']: row for row in client.get('/api/grants').json()['grants']}
    assert [rows[identifier]['status'] for identifier in grants] == ['active', 'expired', 'expired', 'expired', 'pending', 'revoked']
    assert rows[cleaned]['expires'] is None and rows[used]['expires'] is None
    store.execute('DELETE FROM tokens WHERE grant_id=?', (expired,))
    store.execute('DELETE FROM oauth_codes WHERE grant_id=?', (pending,))
    rows = {row['id']: row for row in client.get('/api/grants').json()['grants']}
    assert rows[expired]['status'] == rows[pending]['status'] == 'expired'
    assert all('pending_until' not in row for row in rows.values())
