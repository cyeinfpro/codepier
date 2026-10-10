"""Synthetic HTTP clients verify the real onboarding routes and MCP hooks."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from hub.app import create_app
from hub.principal import Principal
from shared.mcp_protocol import MODERN, PREFIX, request_headers
from tests.legacy_iam_fixture import seed_owner


@pytest.fixture
def connection_api(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_PUBLIC_URL', 'http://testserver')
    monkeypatch.setenv('MCP_PUBLIC_URL', '')
    app = create_app(str(tmp_path / 'hub'))
    store, auth = app.state.store, app.state.auth
    seed_owner(store, 'owner', 'owner')
    seed_owner(store, 'other', 'other')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES('node','Fixture',?,?)",
                  (store.encrypt('synthetic-device-secret'), time.time()))
    store.execute("""INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created)
                     VALUES('project','fixture','fixture','node',?,'write',1,?)""",
                  (str(tmp_path / 'project'), time.time()))
    viewer = Principal('panel:owner','owner',{'read','write','execute'},['*'],admin=True)
    grant = auth.issue_grant(viewer, 'Synthetic connection', ['read'], ['project'])
    with store.transaction():
        session = auth.new_session('owner')
        other = auth.new_session('other')
    with TestClient(app) as client:
        client.headers.update({'Cookie':'rd_session=' + session['cookie'],
                              'X-RD-CSRF':session['csrf'], 'X-CodePier-Space':'legacy'})
        yield app, client, grant, other


def rpc(client, grant, method, params=None, modern=False, identifier=1):
    body = {'jsonrpc':'2.0','id':identifier,'method':method,'params':dict(params or {})}
    headers = {'Authorization':'Bearer ' + grant['token'],
               'Accept':'application/json, text/event-stream'}
    if modern:
        body['params']['_meta'] = {PREFIX+'protocolVersion':MODERN, PREFIX+'clientCapabilities':{}}
        headers.update(request_headers(body))
    return client.post('/mcp', json=body, headers=headers)


def status(client, grant):
    return client.get('/api/grants/' + grant['grant_id'] + '/connection-status',
                      params={'project_id':'project'})


@pytest.mark.parametrize('modern', [False, True])
def test_discovery_and_catalog_evidence_are_real_server_observations(connection_api, modern):
    app, client, grant, _ = connection_api
    initial = status(client, grant).json()
    assert next(x for x in initial['layers'] if x['id']=='discovery')['status'] == 'unknown'
    response = rpc(client, grant, 'server/discover' if modern else 'initialize',
                   {} if modern else {'protocolVersion':'2025-11-25'}, modern)
    assert response.status_code == 200, response.text
    catalog = rpc(client, grant, 'tools/list', modern=modern).json()['result']
    observed = status(client, grant).json()
    assert observed['catalog']['last_served_sha256'] == catalog['_meta']['com.codepier/catalogSha256']
    assert observed['catalog']['last_served_matches_current'] is True
    assert observed['catalog']['host_scan_status'] == observed['catalog']['client_cache_status'] == 'unknown'
    assert next(x for x in observed['layers'] if x['id']=='discovery')['status'] == 'observed'
    assert next(x for x in observed['layers'] if x['id']=='readonly')['status'] == 'unknown'
    assert catalog['_meta']['com.codepier/toolContractProtocol'] == observed['contract_protocol']
    assert grant['token'] not in json.dumps(observed)
    assert not app.state.store.all('SELECT * FROM operations')


def test_legacy_ping_stays_empty_and_oversized_rpc_identity_is_rejected(connection_api):
    _, client, grant, _ = connection_api
    assert rpc(client, grant, 'ping').json()['result'] == {}
    response = rpc(client, grant, 'ping', identifier='x'*1025)
    assert response.status_code == 400
    assert response.json()['id'] is None
    assert response.json()['error']['message'] == 'Request ID is too large'


def test_connection_status_enforces_owner_and_tunnel_preview_is_csrf_guarded(connection_api):
    app, client, grant, other = connection_api
    url = '/api/grants/' + grant['grant_id'] + '/connection-status'
    assert client.get(url, headers={'Cookie':'rd_session='+other['cookie']}).status_code == 404
    assert client.get(url, headers={'X-CodePier-Space':'missing'}).status_code == 404
    payload = {'tunnel_id':'tunnel_'+'a'*32, 'profile':'fixture',
               'install_dir':'/opt/codepier', 'hub_url':'http://127.0.0.1:8765'}
    before = app.state.store.one('SELECT count(*) AS n FROM grants')['n']
    response = client.post('/api/connection/tunnel-preview', json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['configuration_valid'] and result['verification_status']=='not_run'
    assert result['connection_state']=='unknown' and result['effective_scope']=='unverified'
    assert app.state.store.one('SELECT count(*) AS n FROM grants')['n'] == before
    assert client.post('/api/connection/tunnel-preview', json=payload,
                       headers={'X-RD-CSRF':''}).status_code == 403
    invalid = client.post('/api/connection/tunnel-preview', json={**payload,'api_key':'SHOULD_NOT_ECHO'})
    assert invalid.status_code == 200 and invalid.json()['configuration_valid'] is False
    assert 'SHOULD_NOT_ECHO' not in invalid.text


def test_revocation_stops_requests_and_no_host_claim_can_mark_read_success(connection_api):
    app, client, grant, _ = connection_api
    response = rpc(client, grant, 'tools/list', {'_meta':{'read_success':True,'host_scan_complete':True}})
    assert response.status_code == 200
    assert next(x for x in status(client, grant).json()['layers'] if x['id']=='readonly')['status']=='unknown'
    app.state.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (grant['grant_id'],))
    assert rpc(client, grant, 'tools/list').status_code == 401
    value = status(client, grant).json()
    assert next(x for x in value['layers'] if x['id']=='authentication')['status']=='blocked'
