"""Real HTTP bearer and owner-CSRF gates; synthetic projects only."""
import json
import time

from starlette.requests import Request
import pytest
from shared.util import DevError

from tests.collaboration_support import key
from tests.test_collaboration_delegation_bridge import bridge as bridge, panel, tool


def test_bearer_token_id_never_bypasses_its_grant_bound_room_ceiling(bridge):
    b = bridge
    store = b.app.state.store
    row = store.one("SELECT * FROM projects WHERE id='project'")
    store.execute('''INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created)
        VALUES ('otherproject','other-fixture','other-fixture',?,?,'','write',1,?)''',
        (row['device_id'], row['root'] + '-other-unopened-fixture', time.time()))
    store.execute('UPDATE grants SET projects=? WHERE id=?', (json.dumps(['project', 'otherproject']), b.grant))
    request = Request({'type': 'http', 'headers': [(b'authorization', ('Bearer ' + b.token).encode())]})
    assert set(b.app.state.auth.bearer(request).projects) == {'project', 'otherproject'}
    saved = panel(b, 'room-scope', {**b.scope, 'conversation_id': b.room['id'], 'grant_id': b.grant,
        'expected_version': 0, 'project_rules': [{'project_id': 'project', 'capabilities': ['read']}],
        'confirm_connection_wide_ceiling': True, 'idempotency_key': key()})
    assert saved['credential_bound']
    assert b.app.state.auth.bearer(request).projects == ['project']
    # Exercise the published compact catalog, not hidden legacy primitive names.
    assert [r['id'] for r in tool(b, 'project_query', {'operation': 'list'})['projects']] == ['project']
    result = tool(b, 'read', {'project': 'otherproject', 'path': 'README.md'}, ok=False)
    assert result and not store.all("SELECT * FROM operations WHERE tool='read'")
    response = b.client.post('/api/collaboration/room-scope-control', headers={'X-RD-CSRF': 'wrong'},
        json={**b.scope, 'grant_id': b.grant, 'expected_version': 1, 'action': 'revoke', 'idempotency_key': key()})
    assert response.status_code == 403
    assert store.one('SELECT state FROM room_scope_manifests WHERE grant_id=?', (b.grant,))['state'] == 'active'


@pytest.mark.parametrize('name', ['download_artifact', 'inspect_file_source'])
def test_real_grant_scope_blocks_native_adapter_before_source_access(bridge, monkeypatch, name):
    b = bridge
    panel(b, 'room-scope', {**b.scope, 'conversation_id': b.room['id'], 'grant_id': b.grant,
        'expected_version': 0, 'project_rules': [{'project_id': 'project', 'capabilities': ['read']}],
        'confirm_connection_wide_ceiling': True, 'idempotency_key': key()})
    actor = b.app.state.auth.bearer(Request({'type': 'http', 'headers': [
        (b'authorization', ('Bearer ' + b.token).encode())]}))
    from hub import native_file_ingress as native
    monkeypatch.setenv('CODEPIER_FILE_IMPORT_STREAMING', 'true')
    monkeypatch.setenv('CODEPIER_NATIVE_FILE_RELAY', 'true')
    calls = []
    async def prohibited(*args):
        calls.append(args)
        pytest.fail('A room-scoped credential must not enter a native source adapter')
    monkeypatch.setattr(native, 'import_native_file', prohibited)
    monkeypatch.setattr(native, 'inspect_native_file', prohibited)
    # Use the actual Runtime and its database worker on the TestClient loop,
    # not the lightweight routing fixture that originally lacked Principal.
    with pytest.raises(DevError) as denied:
        b.client.portal.call(b.app.state.runtime.invoke, name,
            {'project': 'project', 'file': {'file_id': 'synthetic',
              'download_url': 'https://files.oaiusercontent.com/synthetic-unrequested'},
             'idempotency_key': key()}, actor)
    assert denied.value.code == 'ROOM_SCOPE_WORKSPACE_DENIED'
    assert not calls
    assert not b.app.state.store.all('SELECT * FROM operations')
