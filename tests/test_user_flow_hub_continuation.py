"""F13/F14/F19/F31 at the real HTTP/SQLite boundary, without any real Agent."""
import json
import time

import pytest

from tests.test_audit_api import api, rpc  # noqa: F401


def mapping(**changes):
    return {'alias':'NewMapping','device_id':'device','root':'/tmp/new-fixture',
            'mode':'write','allow_tasks':False,'idempotency_key':'fixture-project-save',**changes}


def test_slow_validation_reuses_actual_operation_and_commits_only_once(api):
    app, client, _ = api
    body = mapping()
    first = client.post('/api/projects', json=body)
    second = client.post('/api/projects', json=body)
    assert first.status_code == second.status_code == 409
    one, two = first.json()['error'], second.json()['error']
    assert one['code'] == two['code'] == 'VALIDATION_PENDING'
    assert one['operation_id'] == two['operation_id']
    store = app.state.store
    assert store.one("SELECT count(*) AS n FROM operations WHERE tool='system_validate'")['n'] == 1
    assert not store.one("SELECT id FROM projects WHERE alias='NewMapping'")
    store.execute("UPDATE operations SET state='succeeded',result=? WHERE id=?", (
        json.dumps({'ok':True,'data':{'root':body['root'],'writable':True,'allow_tasks':False}}),
        one['operation_id']))
    done = client.post('/api/projects', json=body)
    again = client.post('/api/projects', json=body)
    assert done.status_code == again.status_code == 200, (done.text,again.text)
    assert done.json()['id'] == again.json()['id']
    assert store.one("SELECT count(*) AS n FROM projects WHERE alias='NewMapping'")['n'] == 1
    assert store.one("SELECT count(*) AS n FROM operations WHERE tool='system_validate'")['n'] == 1
    conflict = client.post('/api/projects',json={**body,'root':'/tmp/different'})
    assert conflict.status_code == 409 and conflict.json()['error']['code']=='IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('change',['project','alias','device','login'])
def test_validation_cannot_commit_after_its_authority_or_target_changes(api, monkeypatch, change):
    app, client, _ = api
    store = app.state.store
    body = mapping(alias='fixture' if change=='project' else 'NewMapping')
    async def validation(*_args, **_kwargs):
        if change == 'project':
            store.execute("UPDATE projects SET description='newer window' WHERE id='project'")
        elif change == 'alias':
            store.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)',
                          ('other-window','NewMapping','newmapping','device','/tmp/other','newer window','write',0,time.time()))
        elif change == 'device':
            store.execute("UPDATE devices SET enabled=0 WHERE id='device'")
        else:
            store.execute('DELETE FROM sessions')
        return {'root':body['root'],'writable':True,'allow_tasks':False}
    monkeypatch.setattr(app.state.runtime,'dispatch',validation)
    response = (client.put('/api/projects/project',json=body) if change=='project'
                else client.post('/api/projects',json=body))
    expected = {'project':'PROJECT_CHANGED','alias':'ALIAS_EXISTS',
                'device':'DEVICE_DISABLED','login':'UNAUTHENTICATED'}
    assert response.status_code in {401,409}, response.text
    error = response.json()['error']['code']
    if change == 'login':
        assert response.status_code == 401
    else:
        assert error == expected[change]
    if change == 'project':
        assert store.one("SELECT description FROM projects WHERE id='project'")['description']=='newer window'
    else:
        assert not store.one("SELECT id FROM projects WHERE root=?",(body['root'],))


def test_key_rotation_preserves_the_user_selected_agent_url(api):
    _app, client, _ = api
    selected = 'https://agent-facing.fixture:9443'
    created = client.post('/api/devices',json={'name':'Different route','hub_url':selected})
    assert created.status_code == 200, created.text
    original = created.json()['pairing']
    rotated = client.post('/api/devices/'+original['device_id']+'/rotate')
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()['pairing']['hub_url'] == selected
    assert rotated.json()['pairing']['secret'] != original['secret']


@pytest.mark.parametrize('kind',['workspace','changes'])
def test_legacy_app_resource_is_readable_through_actual_mcp_router(api, kind):
    _app, client, pat = api
    def read(brand):
        return rpc(client,pat,{'jsonrpc':'2.0','id':1,'method':'resources/read',
            'params':{'uri':f'ui://{brand}/{kind}-v1.html'}}).json()
    legacy, current = read('relay'), read('codepier')
    assert 'error' not in legacy and 'error' not in current
    a,b = legacy['result']['contents'][0],current['result']['contents'][0]
    assert a['text'] == b['text'] and 'CodePier' in a['text']
    assert a['uri'].startswith('ui://relay/') and b['uri'].startswith('ui://codepier/')


def test_browser_offline_is_definitively_rejected_before_an_operation_exists(api, monkeypatch):
    app, client, _ = api
    monkeypatch.setattr(app.state.runtime.integrations,'guard',lambda *_a,**_k:None)
    response = client.post('/api/tools/call',json={'tool':'browser_open','arguments':{
        'project':'fixture','url':'https://example.test/','idempotency_key':'offline-fixture-only'}})
    assert response.status_code == 409, response.text
    error = response.json()['error']
    assert error['code']=='COMPUTER_OFFLINE' and error['admitted'] is False
    assert not error.get('operation_id')
    assert not app.state.store.one("SELECT id FROM operations WHERE idem='offline-fixture-only'")
