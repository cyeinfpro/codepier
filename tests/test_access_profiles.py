"""Profile ceilings, stable identities and old-grant compatibility over real HTTP."""
import base64
import hashlib
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import jsonschema
import pytest

from hub.access_profiles import effective_grant
from hub.store import Store
from shared.crypto import digest
from shared.util import DevError
from tests.test_audit_api import api as api
from tests.test_continuous_access import add_project, grant_for, update_range, visible_projects


def create(client, label='Review', scopes=None, projects=None, **extra):
    body = {'label': label, 'scopes': scopes or ['read'], 'projects': projects or ['project'],
            'idempotency_key': uuid.uuid4().hex, **extra}
    response = client.post('/api/access-profiles', json=body)
    assert response.status_code == 201, response.text
    return response.json()


def change(client, profile, **values):
    body = {k: profile[k] for k in ('label', 'scopes', 'enabled')}
    body.update(projects=[] if profile['all_projects'] else profile['projects'],
                all_projects=profile['all_projects'], expected_version=profile['version'])
    body.update(values)
    return client.put('/api/access-profiles/' + profile['id'], json=body)


def pat(client, profile, scopes=None, projects=None, **extra):
    response = client.post('/api/grants', json={'label': 'profile connection',
        'scopes': scopes or profile['scopes'], 'projects': projects or profile['projects'],
        'profile_id': profile['id'], 'profile_version': profile['version'], **extra})
    assert response.status_code == 200, response.text
    return response.json()


def call(client, token, name='get_profile', arguments=None, endpoint='/mcp'):
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': name, 'arguments': arguments or {}}}
    return client.post(endpoint, json=payload, headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'})


def data(response):
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert not result.get('isError'), result
    assert json.loads(result['content'][0]['text']) == result['structuredContent']
    return result['structuredContent']


def start_oauth(client, scopes='read write execute computer'):
    client_id = client.post('/oauth/register', json={'redirect_uris': ['http://localhost:12345/callback']}).json()['client_id']
    verifier = 'v' * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    params = {'response_type': 'code', 'client_id': client_id, 'redirect_uri': 'http://localhost:12345/callback',
              'scope': scopes, 'code_challenge': challenge, 'code_challenge_method': 'S256',
              'resource': 'http://testserver/mcp'}
    response = client.get('/oauth/authorize', params=params, follow_redirects=False)
    assert response.status_code == 303, response.text
    identifier = parse_qs(urlsplit(response.headers['location']).query)['authorize'][0]
    return client_id, identifier, verifier


def oauth(client, profile, scopes=None, projects=None):
    client_id, request_id, verifier = start_oauth(client)
    response = client.post('/api/oauth/requests/' + request_id + '/decide', json={
        'allow': True, 'scopes': scopes or profile['scopes'], 'projects': projects or profile['projects'],
        'profile_id': profile['id'], 'profile_version': profile['version']})
    assert response.status_code == 200, response.text
    code = parse_qs(urlsplit(response.json()['redirect']).query)['code'][0]
    response = client.post('/oauth/token', data={'grant_type': 'authorization_code', 'code': code,
        'client_id': client_id, 'code_verifier': verifier, 'redirect_uri': 'http://localhost:12345/callback'})
    assert response.status_code == 200, response.text
    return client_id, response.json()


@pytest.mark.parametrize('endpoint', ['/mcp', '/mcp?profile=coding'])
def test_profile_contract_is_discoverable_and_credential_bound(api, endpoint):
    app, client, _ = api
    profile = create(client)
    credential = pat(client, profile)['token']
    response = client.post(endpoint, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        headers={'Authorization': 'Bearer ' + credential, 'Accept': 'application/json, text/event-stream'})
    catalog = response.json()['result']['tools']
    tools = {tool['name']: tool for tool in catalog}
    assert [tool['name'] for tool in catalog if tool['_meta'].get('openai/profile')] == ['get_profile']
    schema = tools['get_profile']
    assert schema['inputSchema']['properties'] == {} and schema['inputSchema']['additionalProperties'] is False
    assert schema['annotations']['readOnlyHint'] is True and schema['annotations']['destructiveHint'] is False
    assert schema['annotations']['openWorldHint'] is False
    assert schema['outputSchema']['additionalProperties'] is False
    assert schema['outputSchema']['required'] == ['id']
    assert set(schema['outputSchema']['properties']) == {'id', 'name', 'nickname'}
    identity = data(call(client, credential, endpoint=endpoint))
    jsonschema.validate(identity, schema['outputSchema'])
    assert identity == {'id': profile['id'], 'name': 'CodePier', 'nickname': 'Review'}
    refused = call(client, credential, arguments={'profile_id': 'someone-else'}, endpoint=endpoint).json()['result']
    assert refused['isError'] and 'structuredContent' not in refused
    assert json.loads(refused['content'][0]['text'])['error']['code'] == 'INVALID_ARGUMENTS'
    assert call(client, 'not-a-token', endpoint=endpoint).status_code == 401
    assert app.state.store.one('SELECT count(*) AS n FROM operations')['n'] == 0


def test_create_is_idempotent_and_never_reassigns_identity(api):
    _, client, _ = api
    body = {'label': 'Review', 'scopes': ['read'], 'projects': ['project'], 'idempotency_key': 'creation-identity-1'}
    first = client.post('/api/access-profiles', json=body)
    second = client.post('/api/access-profiles', json=body)
    assert first.status_code == second.status_code == 201 and first.json() == second.json()
    assert client.post('/api/access-profiles', json={**body, 'label': 'different'}).status_code == 409
    assert client.post('/api/access-profiles', json={**body, 'idempotency_key': 'new-key-123'}).status_code == 409
    profile = first.json()
    credential = pat(client, profile)['token']
    renamed = change(client, profile, label='Changed name')
    assert renamed.status_code == 200
    assert renamed.json()['id'] == profile['id'] and renamed.json()['version'] == 2
    assert data(call(client, credential))['nickname'] == 'Changed name'
    other = create(client, label='Review')
    assert other['id'] != profile['id']


def test_refresh_reconnect_and_scope_upgrade_keep_profile_id_but_not_grant_ownership(api):
    app, client, _ = api
    profile = create(client, scopes=['read', 'write'])
    client_id, first = oauth(client, profile, scopes=['read'])
    first_grant = grant_for(app, first['access_token'])
    refreshed = client.post('/oauth/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
                                                'refresh_token': first['refresh_token']})
    assert refreshed.status_code == 200, refreshed.text
    assert data(call(client, refreshed.json()['access_token']))['id'] == profile['id']
    _, second = oauth(client, profile, scopes=['read', 'write'])
    second_grant = grant_for(app, second['access_token'])
    assert first_grant['id'] != second_grant['id']
    assert data(call(client, second['access_token']))['id'] == profile['id']
    assert data(call(client, first['access_token'], 'get_access_context'))['scopes'] == ['read']
    runtime = app.state.runtime
    now = time.time()
    app.state.store.execute('''INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,
        fingerprint,state,created,updated) VALUES (?,?,?,?,?,?,'{}','','succeeded',?,?)''',
        ('op-private', 'device', 'project', 'mcp:' + first_grant['id'], first_grant['id'], 'fs_read', now, now))
    refused = call(client, second['access_token'], 'operations_get', {'operation_id': 'op-private'}).json()['result']
    assert refused['isError'] and refused['structuredContent']['error']['code'] == 'OPERATION_NOT_FOUND'
    assert runtime is not None


def test_profile_and_original_consent_are_intersected_never_widened(api):
    app, client, _ = api
    add_project(app)
    profile = create(client)
    token = pat(client, profile)['token']
    expanded = change(client, profile, scopes=['read', 'write', 'execute'], projects=['project', 'future'])
    assert expanded.status_code == 200
    assert visible_projects(client, token) == {'project'}
    context = data(call(client, token, 'get_access_context'))
    assert context['scopes'] == ['read'] and context['profile']['id'] == profile['id']
    assert context['chat_project_is_security_boundary'] is False
    assert 'root' not in json.dumps(context) and 'grant_id' not in json.dumps(context)
    denied = call(client, token, 'shell_exec', {'project': 'fixture', 'command': 'never run',
        'idempotency_key': 'no-execution-123'}).json()['result']
    assert denied['isError'] and denied['structuredContent']['error']['code'] == 'INSUFFICIENT_SCOPE'
    assert app.state.store.one('SELECT count(*) AS n FROM operations')['n'] == 0


def test_profile_shrink_disables_queued_admission_and_refresh_without_killing_work(api):
    app, client, _ = api
    store = app.state.store
    store.execute('UPDATE projects SET allow_tasks=1')
    profile = create(client, scopes=['read', 'write', 'execute'])
    client_id, tokens = oauth(client, profile)
    grant = grant_for(app, tokens['access_token'])
    project = store.one('SELECT * FROM projects WHERE id=?', ('project',))
    operation = {'project_id': 'project', 'device_id': 'device', 'tool': 'shell_exec', 'grant_id': grant['id']}
    request = {'project': project}
    assert app.state.runtime.permission_error(operation, request) is None
    narrowed = change(client, profile, scopes=['read']).json()
    assert app.state.runtime.permission_error(operation, request) is not None
    assert data(call(client, tokens['access_token'], 'get_access_context'))['scopes'] == ['read']
    assert change(client, narrowed, enabled=False).status_code == 200
    assert call(client, tokens['access_token']).status_code == 401
    refresh_before = store.one('SELECT * FROM tokens WHERE hash=?', (digest(tokens['refresh_token']),))
    denied = client.post('/oauth/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
        'refresh_token': tokens['refresh_token']})
    assert denied.status_code == 400 and denied.json()['error'] == 'invalid_grant'
    assert store.one('SELECT * FROM tokens WHERE hash=?', (digest(tokens['refresh_token']),)) == refresh_before
    assert store.one('SELECT revoked FROM grants WHERE id=?', (grant['id'],))['revoked'] == 0


def test_disable_between_consent_and_code_exchange_does_not_mint_tokens(api):
    app, client, _ = api
    profile = create(client)
    client_id, identifier, verifier = start_oauth(client)
    response = client.post('/api/oauth/requests/' + identifier + '/decide', json={'allow': True,
        'scopes': ['read'], 'projects': ['project'], 'profile_id': profile['id'], 'profile_version': profile['version']})
    code = parse_qs(urlsplit(response.json()['redirect']).query)['code'][0]
    assert change(client, profile, enabled=False).status_code == 200
    count = app.state.store.one('SELECT count(*) AS n FROM tokens')['n']
    response = client.post('/oauth/token', data={'grant_type': 'authorization_code', 'client_id': client_id,
        'redirect_uri': 'http://localhost:12345/callback', 'code_verifier': verifier, 'code': code})
    assert response.status_code == 400 and response.json()['error'] == 'invalid_grant'
    assert app.state.store.one('SELECT count(*) AS n FROM tokens')['n'] == count


def test_oauth_rejects_stale_overbroad_and_unrequested_profile_permissions(api):
    app, client, _ = api
    profile = create(client)
    _, identifier, _ = start_oauth(client, scopes='read write')
    endpoint = '/api/oauth/requests/' + identifier + '/decide'
    base = {'allow': True, 'scopes': ['read'], 'projects': ['project'],
            'profile_id': profile['id'], 'profile_version': profile['version']}
    assert client.post(endpoint, json={**base, 'scopes': ['read', 'write']}).status_code == 403
    assert client.post(endpoint, json={**base, 'scopes': ['read', 'computer']}).status_code == 400
    assert client.post(endpoint, json={**base, 'projects': [], 'all_projects': True}).status_code == 403
    assert client.post(endpoint, json={**base, 'profile_version': 88}).status_code == 409
    assert app.state.store.one('SELECT used FROM oauth_requests WHERE id=?', (identifier,))['used'] == 0
    assert client.post(endpoint, json=base).status_code == 200


def test_cross_profile_selection_and_cross_owner_access_are_rejected(api):
    app, client, _ = api
    add_project(app)
    first = create(client)
    other = create(client, label='Other', projects=['future'])
    token = pat(client, first)['token']
    assert visible_projects(client, token) == {'project'}
    assert data(call(client, token))['id'] != other['id']
    app.state.store.execute('INSERT INTO users VALUES (?,?,?,?)', ('other', 'other-owner', 'unused', time.time()))
    app.state.store.execute('UPDATE access_profiles SET user_id=? WHERE id=?', ('other', other['id']))
    assert client.get('/api/access-profiles/' + other['id']).status_code == 404
    assert len(client.get('/api/access-profiles').json()['profiles']) == 1
    response = client.post('/api/grants', json={'label': 'wrong owner', 'scopes': ['read'], 'projects': ['future'],
        'profile_id': other['id'], 'profile_version': other['version']})
    assert response.status_code == 404
    # Even an inconsistent stored binding cannot borrow another owner's profile.
    row = grant_for(app, token)
    app.state.store.execute('UPDATE grants SET profile_id=? WHERE id=?', (other['id'], row['id']))
    assert call(client, token).status_code == 401


def test_legacy_bulk_expansion_cannot_rewrite_profile_managed_grants(api):
    app, client, legacy_pat = api
    profile = create(client)
    _, tokens = oauth(client, profile)
    before = grant_for(app, tokens['access_token'])
    response = client.put('/api/settings/access', json={'all_projects': True, 'apply_to_existing': True})
    assert response.status_code == 200 and response.json()['updated_grants'] == 0
    assert grant_for(app, tokens['access_token']) == before
    response = update_range(client, before['id'], all_projects=True)
    assert response.status_code == 409 and response.json()['error']['code'] == 'PROFILE_MANAGED_GRANT'
    add_project(app)
    assert visible_projects(client, tokens['access_token']) == {'project'}
    # Existing legacy identity remains account-based, not a made-up per-token ID.
    old_identity = data(call(client, legacy_pat))
    second = client.post('/api/grants', json={'label': 'another legacy', 'scopes': ['read'], 'projects': ['project']}).json()
    assert data(call(client, second['token'])) == old_identity
    assert data(call(client, legacy_pat, 'get_access_context'))['managed'] is False


def test_stale_edit_aba_and_disable_do_not_restore_revoked_grants(api):
    app, client, _ = api
    profile = create(client)
    credential = pat(client, profile)
    widened = change(client, profile, scopes=['read', 'write']).json()
    restored = change(client, widened, scopes=['read']).json()
    assert change(client, profile, scopes=['read', 'execute']).status_code == 409
    assert client.delete('/api/grants/' + credential['grant_id']).status_code == 200
    disabled = change(client, restored, enabled=False).json()
    assert change(client, disabled, enabled=True).status_code == 200
    assert call(client, credential['token']).status_code == 401
    assert app.state.store.one('SELECT revoked FROM grants WHERE id=?', (credential['grant_id'],))['revoked'] == 1


@pytest.mark.parametrize('field,value', [('enabled', 'true'), ('all_projects', 1), ('scopes', ['read','admin']),
    ('scopes', ['write']), ('label', '   '), ('projects', ['*']), ('projects', ['missing'])])
def test_profile_invalid_inputs_never_create_a_record(api, field, value):
    app, client, _ = api
    body = {'label': 'bad', 'scopes': ['read'], 'projects': ['project'], 'idempotency_key': 'invalid-create-123', field: value}
    response = client.post('/api/access-profiles', json=body)
    assert response.status_code in {400, 422}, response.text
    assert app.state.store.one('SELECT count(*) AS n FROM access_profiles')['n'] == 0


def test_management_requires_owner_session_csrf_and_same_origin(api):
    app, client, _ = api
    body = {'label': 'blocked', 'scopes': ['read'], 'projects': ['project'], 'idempotency_key': 'blocked-create-123'}
    assert client.post('/api/access-profiles', json=body, headers={'X-RD-CSRF': 'wrong'}).status_code == 403
    assert client.post('/api/access-profiles', json=body, headers={'Origin': 'https://other.example'}).status_code == 403
    client.cookies.clear()
    assert client.post('/api/access-profiles', json=body).status_code == 401
    assert client.get('/api/access-profiles').status_code == 401
    assert app.state.store.one('SELECT count(*) AS n FROM access_profiles')['n'] == 0


def test_explicit_profile_wildcard_still_requires_wildcard_consent(api):
    app, client, _ = api
    profile = create(client, projects=[], all_projects=True)
    assert profile['projects'] == ['*']
    response = client.post('/api/grants', json={'label': 'all', 'scopes': ['read'], 'all_projects': True,
        'profile_id': profile['id'], 'profile_version': profile['version']})
    assert response.status_code == 200
    limited = pat(client, profile, projects=['project'])['token']
    add_project(app)
    assert visible_projects(client, response.json()['token']) == {'project','future'}
    assert visible_projects(client, limited) == {'project'}


def test_profile_validation_does_not_trust_malformed_stored_scopes(api):
    app, client, _ = api
    profile = create(client)
    token = pat(client, profile)['token']
    app.state.store.execute('UPDATE access_profiles SET scopes=? WHERE id=?', ('invalid json', profile['id']))
    assert call(client, token).status_code == 401
    with pytest.raises(DevError):
        effective_grant(app.state.store, grant_for(app, token))


def test_v5_migration_keeps_legacy_grants_tokens_and_master_key(tmp_path):
    directory = tmp_path / 'legacy'
    from tests.legacy_iam_fixture import legacy_store
    store = legacy_store(directory)
    store.execute('DROP INDEX grants_profile')
    store.execute('ALTER TABLE grants DROP COLUMN profile_id')
    store.execute('DROP TABLE access_profiles')
    store.execute("UPDATE meta SET value='5' WHERE key='schema'")
    store.execute("INSERT INTO users VALUES ('owner','legacy-owner','!fixture-only',1)")
    store.execute('''INSERT INTO grants(id,user_id,label,scopes,projects,created)
        VALUES ('legacy','owner','existing','["read"]','["*"]',1)''')
    store.execute("INSERT INTO tokens VALUES ('token','hash','legacy','pat',9999999999,1)")
    before, tokens = store.one("SELECT * FROM grants WHERE id='legacy'"), store.all('SELECT * FROM tokens')
    key = (directory / 'master.key').read_bytes()
    store.close()
    for _ in range(2):
        upgraded = Store(directory)
        current = upgraded.one("SELECT * FROM grants WHERE id='legacy'")
        assert current.pop('profile_id') is None
        assert current.pop('space_id') == 'legacy'
        assert current.pop('owner_user_id') == 'owner'
        assert current.pop('identity_id') is None
        assert current.pop('user_epoch') == 1
        assert current == before
        assert upgraded.all('SELECT * FROM tokens') == tokens
        assert upgraded.one('SELECT count(*) AS n FROM access_profiles')['n'] == 0
        assert upgraded.one("SELECT value FROM meta WHERE key='schema'")['value'] == '9'
        assert (directory / 'master.key').read_bytes() == key
        upgraded.close()


@pytest.mark.parametrize('tool', ['computer_observe', 'browser_snapshot', 'browser_action'])
def test_narrowing_computer_scope_blocks_original_operation_media(api, tool):
    app, client, _ = api
    profile = create(client, scopes=['read', 'computer'])
    credential = pat(client, profile)
    now = time.time()
    app.state.store.execute('''INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,
        fingerprint,state,result,created,updated) VALUES (?,?,?,?,?,?,'{}','','succeeded',?,?,?)''',
        ('media-private', 'device', 'project', 'mcp:' + credential['grant_id'], credential['grant_id'], tool,
         json.dumps({'ok': True, 'data': {'text': 'PRIVATE_BROWSER_MEDIA'}}), now, now))
    assert change(client, profile, scopes=['read']).status_code == 200
    result = call(client, credential['token'], 'operations_get', {'operation_id': 'media-private'})
    assert 'PRIVATE_BROWSER_MEDIA' not in result.text
    assert result.json()['result']['structuredContent']['error']['code'] == 'INSUFFICIENT_SCOPE'


@pytest.mark.parametrize('field,value', [
    ('scopes', '"read"'), ('scopes', '{"read": true}'), ('scopes', '["read", "administrator"]'),
    ('projects', '"project"'), ('projects', '{"*": true}'), ('projects', '["*", "project"]'),
])
def test_malformed_profile_shapes_fail_closed(api, field, value):
    app, client, _ = api
    profile = create(client)
    credential = pat(client, profile)['token']
    app.state.store.execute(f'UPDATE access_profiles SET {field}=? WHERE id=?', (value, profile['id']))
    assert call(client, credential).status_code == 401


def test_real_durable_queue_rechecks_disabled_profile_before_any_dispatch(api):
    import asyncio
    from hub.runtime import Principal

    app, client, _ = api
    store, runtime = app.state.store, app.state.runtime
    profile = create(client, scopes=['read', 'write'])
    credential = pat(client, profile)
    grant = grant_for(app, credential['token'])
    principal = Principal('mcp:' + grant['id'] + ':' + grant['label'], grant['user_id'],
                          {'read', 'write'}, ['project'], grant_id=grant['id'], profile_id=profile['id'])
    runtime.wait_seconds = 0
    async def scenario():
        result = await runtime.invoke('fs_write', {'project': 'fixture', 'path': 'never-written.txt',
            'content': 'MUST NOT EXECUTE', 'expected_sha256': 'new', 'idempotency_key': 'queued-profile-test'}, principal)
        identifier = result['operation_id']
        before = store.one('SELECT * FROM operations WHERE id=?', (identifier,))
        assert before['attempts'] == 0 and not before['accepted_at']
        store.execute('UPDATE access_profiles SET enabled=0,version=version+1 WHERE id=?', (profile['id'],))
        await runtime.deliver(identifier)
        after = store.one('SELECT * FROM operations WHERE id=?', (identifier,))
        assert after['attempts'] == 0 and not after['accepted_at']
        assert json.loads(after['result'])['error']['code'] == 'AUTHORIZATION_CHANGED'
        with pytest.raises(DevError, match='Profile'):
            runtime.operation(identifier, principal)
    asyncio.run(scenario())
