"""Saved VPS permissions, durable credential references and real MCP transport."""
from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest
from pydantic import ValidationError

from hub.runtime import Principal, Runtime
from hub.store import Store
from hub.vps import ProjectVPSAssignments, VPSAssignments, VPSInput
from shared.contracts import tool_definitions
from shared.core_contracts import Exec
from shared.util import DevError, atomic_json
from tests.test_ssh import PASSWORD, fake_transport


@pytest.fixture
def inventory(tmp_path):
    store = Store(tmp_path / 'data')
    device = uuid.uuid4().hex
    store.execute('INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)', (device, 'Fixture Agent', store.encrypt('fixture-device'), time.time()))
    projects = []
    for alias in ('Alpha', 'Beta', 'Gamma'):
        identifier = uuid.uuid4().hex
        store.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)', (identifier, alias, alias.lower(), device, str(tmp_path / alias), '', 'write', 1, time.time()))
        projects.append(identifier)
    runtime = Runtime(store)
    owner = Principal('panel:fixture', 'owner', {'read', 'write', 'execute'}, [], admin=True)
    yield runtime, owner, projects
    store.close()


def body(projects, **changes):
    return VPSInput(name='广州 VPS', host='vps.example.invalid', password=PASSWORD, project_ids=projects, **changes)


def edit_body(v, **changes):
    keys = ('name', 'host', 'port', 'username', 'host_key_policy', 'provider', 'region', 'system', 'notes', 'enabled', 'project_ids')
    return VPSInput(**{**{k: v[k] for k in keys}, 'expected_version': v['version'], **changes})


def execute_args(project, vps='0'*32, **changes):
    return {'project': project, 'target': 'vps:'+vps, 'yield_seconds': 0, 'command': 'printf fixture-ok', 'idempotency_key': uuid.uuid4().hex, **changes}


def test_saved_credential_encryption_many_to_many_and_visibility(inventory):
    r, owner, projects = inventory
    v = r.vps.save(body(projects[:2]), owner)
    other = r.vps.save(VPSInput(name='Only Gamma', host='other.invalid', password=PASSWORD, project_ids=projects[2:]), owner)
    assert set(v['project_ids']) == set(projects[:2])
    raw = r.store.one('SELECT * FROM vps_connections WHERE id=?', (v['id'],))
    assert raw['secret'] != PASSWORD and r.store.decrypt(raw['secret']) == PASSWORD
    caller = Principal('mcp:one', 'owner', {'read'}, [projects[0]])
    result = r.vps.list({'project': '', 'query': '', 'offset': 0, 'limit': 50}, caller)
    assert len(result['vps']) == 1 and result['vps'][0]['project_ids'] == [projects[0]]
    assert projects[1] not in json.dumps(result) and other['id'] not in json.dumps(result)
    assert PASSWORD not in json.dumps(result) and 'secret' not in result['vps'][0]
    assert PASSWORD not in json.dumps(r.store.all('SELECT * FROM audit'))


@pytest.mark.parametrize('changes', [
    {'host': '-oProxyCommand=x'}, {'host': 'a;id'}, {'host': 'user@host'},
    {'host': 'host:22'}, {'host': 'https://host'}, {'port': True}, {'port': 0},
    {'username': '-root'}, {'password': ''}, {'password': 'hello\nthere'},
    {'host_key_policy': 'no'}, {'name': '  '}, {'project_ids': ['same', 'same']},
])
def test_invalid_inventory_input(changes):
    with pytest.raises(ValidationError):
        VPSInput(**{'name': 'fixture', 'host': 'host.invalid', 'password': PASSWORD, **changes})


def test_update_keeps_password_and_cas_rejects_lost_updates(inventory):
    r, owner, projects = inventory
    v = r.vps.save(body(projects[:1]), owner)
    before = r.store.one('SELECT secret FROM vps_connections WHERE id=?', (v['id'],))['secret']
    updated = r.vps.save(edit_body(v, notes='deploy directory /srv/app'), owner, v['id'])
    assert updated['version'] == 2 and updated['connection_revision'] == 1
    assert before == r.store.one('SELECT secret FROM vps_connections WHERE id=?', (v['id'],))['secret']
    with pytest.raises(DevError, match='其他窗口'):
        r.vps.save(edit_body(v, password='new-fixture'), owner, v['id'])
    assert r.vps.get(v['id'])['notes'] == 'deploy directory /srv/app'
    rotated = r.vps.save(edit_body(updated, password='new-fixture'), owner, v['id'])
    assert rotated['connection_revision'] == 2


def test_invalid_assignment_rolls_back_entire_create_and_duplicate(inventory):
    r, owner, projects = inventory
    with pytest.raises(DevError):
        r.vps.save(body([projects[0], 'missing']), owner)
    assert not r.store.all('SELECT id FROM vps_connections')
    v = r.vps.save(body(projects[:1]), owner)
    with pytest.raises(DevError) as caught:
        r.vps.save(VPSInput(name='Other', host=v['host'], port=v['port'], username=v['username'], password=PASSWORD), owner)
    assert caught.value.code == 'VPS_EXISTS'
    assert len(r.store.all('SELECT id FROM vps_connections')) == 1


def test_ip_port_username_ambiguity_and_single_default(inventory):
    r, owner, projects = inventory
    v = r.vps.save(body(projects[:1]), owner)
    p = r.project(projects[0], owner)
    assert r.vps.reference(execute_args(projects[0]), p)['id'] == v['id']
    v2 = r.vps.save(VPSInput(name='Same host 2222', host=v['host'], port=2222, password=PASSWORD, project_ids=projects[:1]), owner)
    with pytest.raises(DevError) as caught:
        r.vps.reference(execute_args(projects[0], v['host']), p)
    assert caught.value.code == 'VPS_AMBIGUOUS'
    assert r.vps.reference(execute_args(projects[0], v['host'], port=2222), p)['id'] == v2['id']
    with pytest.raises(DevError):
        r.vps.reference(execute_args(projects[0], v['host'], port=22, username='other'), p)
    with pytest.raises(DevError):
        r.vps.reference(execute_args(projects[1], v['id']), r.project(projects[1], owner))


def test_ipv6_and_pagination(inventory):
    r, owner, projects = inventory
    v = r.vps.save(VPSInput(name='IPv6', host='2001:db8:0:0::1', password=PASSWORD, project_ids=projects[:1]), owner)
    assert v['host'] == '2001:db8::1'
    assert r.vps.list({'query': '2001:db8:0:0::1'}, owner)['total'] == 1
    r.vps.save(body(projects[:1]), owner)
    first = r.vps.list({'limit': 1}, owner)
    second = r.vps.list({'limit': 1, 'offset': first['next_offset']}, owner)
    assert first['total'] == 2 and second['next_offset'] is None
    assert first['vps'][0]['id'] != second['vps'][0]['id']


def test_project_side_assignment_preserves_other_projects(inventory):
    r, owner, projects = inventory
    v = r.vps.save(body(projects[:2]), owner)
    other = r.vps.save(VPSInput(name='Second', host='second.invalid', password=PASSWORD, project_ids=projects[1:2]), owner)
    r.vps.project_assign(projects[0], ProjectVPSAssignments(vps_ids=[other['id']], expected_vps_ids=[v['id']]), owner)
    assert r.vps.get(v['id'])['project_ids'] == [projects[1]]
    assert set(r.vps.get(other['id'])['project_ids']) == set(projects[:2])
    with pytest.raises(DevError):
        r.vps.project_assign(projects[0], ProjectVPSAssignments(vps_ids=[], expected_vps_ids=[v['id']]), owner)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unassign', 'remove_reassign', 'disable', 'password', 'endpoint', 'delete'])
async def test_queued_reference_revocation_blocks_before_connection(inventory, change):
    r, owner, projects = inventory
    v = r.vps.save(body(projects[:1]), owner)
    call = execute_args(projects[0], v['id'])
    receipt = await r.invoke('exec', call, owner)
    op = r.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    request = json.loads(r.store.decrypt(op['payload']))
    assert 'password' not in json.dumps(request) and PASSWORD not in json.dumps(request)
    if change in {'unassign', 'remove_reassign'}:
        updated = r.vps.assign(v['id'], VPSAssignments(project_ids=[], expected_version=v['version']), owner)
        if change == 'remove_reassign':
            r.vps.assign(v['id'], VPSAssignments(project_ids=projects[:1], expected_version=updated['version']), owner)
    elif change == 'disable': r.vps.save(edit_body(v, enabled=False), owner, v['id'])
    elif change == 'password': r.vps.save(edit_body(v, password='rotated-fixture'), owner, v['id'])
    elif change == 'endpoint': r.vps.save(edit_body(v, host='new.invalid'), owner, v['id'])
    else: r.vps.delete(v['id'], v['version'], owner)
    assert r.permission_error(op, request)
    await r.deliver(op['id'])
    result = r.store.one('SELECT * FROM operations WHERE id=?', (op['id'],))
    assert result['state'] == 'failed' and result['attempts'] == 0 and result['payload'] is None


@pytest.mark.asyncio
async def test_idempotency_reference_survives_metadata_edits_and_cancellation(inventory):
    r, owner, projects = inventory
    v = r.vps.save(body(projects[:1]), owner)
    call = execute_args(projects[0], v['id'])
    first = await r.invoke('exec', call, owner)
    r.vps.save(edit_body(v, name='Renamed', notes='metadata change'), owner, v['id'])
    replay = await r.invoke('exec', call, owner)
    assert replay['operation_id'] == first['operation_id']
    with pytest.raises(DevError) as caught:
        await r.invoke('exec', {**call, 'command': 'another command'}, owner)
    assert caught.value.code == 'IDEMPOTENCY_CONFLICT'
    result = await r.cancel(first['operation_id'], owner)
    assert result['state'] == 'cancelled'


def test_transport_credential_not_in_saved_payload_and_delete_cascades(inventory):
    r, owner, projects = inventory
    v = r.vps.save(body(projects[:2]), owner)
    args = Exec(**execute_args(projects[0], v['id'])).model_dump()
    request = {'tool': 'exec', 'args': args, 'project': {'root': '/fixture'}, 'vps_ref': r.vps.reference({**args, 'vps': v['id']}, r.project(projects[0], owner))}
    packet = r.vps.transport(request, projects[0])
    assert packet['tool'] == 'exec' and packet['core_ssh']['password'] == PASSWORD
    assert 'vps_ref' not in packet and PASSWORD not in json.dumps(request)
    r.store.execute('DELETE FROM projects WHERE id=?', (projects[0],))
    assert r.vps.get(v['id'])['project_ids'] == [projects[1]]
    r.vps.delete(v['id'], v['version'], owner)
    assert not r.store.all('SELECT * FROM vps_projects')


def test_missing_master_key_with_only_vps_credentials(inventory):
    r, owner, projects = inventory
    r.vps.save(body([]), owner)
    r.store.execute('DELETE FROM projects')
    r.store.execute('DELETE FROM devices')
    directory = r.store.directory
    (directory / 'master.key').unlink()
    with pytest.raises(RuntimeError, match='master.key'):
        Store(directory)


@pytest.mark.parametrize('profile', ['full', 'coding'])
def test_catalog_has_secret_free_inputs(profile):
    tools = {t['name']: t for t in tool_definitions(profile)}
    for name in ['vps', 'exec']:
        assert name in tools and 'password' not in tools[name]['inputSchema']['properties']
    assert tools['vps']['annotations']['readOnlyHint']
    assert not tools['exec']['annotations']['readOnlyHint']
    assert tools['exec']['annotations']['destructiveHint']
    with pytest.raises(ValidationError):
        Exec(**execute_args('p', password='not-accepted'))


@pytest.fixture(scope='module')
def vps_stack(stack):
    stack.stop_agent()
    stack.config['shell'] = {'enabled': True, 'projects': ['Imago','Nexus','Lumen'], 'command': ['/bin/sh','-c'], 'env': {'PATH': fake_transport(stack.directory / 'vps-bin')}}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    return stack


def create_remote(stack, **changes):
    payload = {'name': 'Fixture-'+uuid.uuid4().hex[:8], 'host': 'vps.example.invalid', 'port': 10000+int(uuid.uuid4().hex[:3],16), 'password': PASSWORD, 'project_ids': [stack.project['id']], **changes}
    return stack.must(stack.client.post('/api/vps', json=payload))


def test_http_admin_csrf_validation_no_secret_echo(vps_stack):
    s = vps_stack
    assert httpx.get(s.url+'/api/vps',trust_env=False).status_code == 401
    headers = dict(s.client.headers);s.client.headers.pop('X-RD-CSRF')
    try:
        denied = s.client.post('/api/vps',json={'name':'No CSRF','host':'host.invalid','password':PASSWORD})
        assert denied.status_code == 403
    finally:s.client.headers.update(headers)
    invalid = s.client.post('/api/vps',json={'name':'Bad','host':'host.invalid','password':PASSWORD+'\n'})
    assert invalid.status_code == 422 and PASSWORD not in invalid.text
    v = create_remote(s)
    response = s.client.get('/api/vps/'+v['id'])
    assert response.headers['cache-control'] == 'no-store'
    assert PASSWORD not in response.text and 'secret' not in response.json()


def test_mcp_saved_ssh_real_process_redaction_and_once_only(vps_stack):
    s = vps_stack
    v = create_remote(s)
    filename = 'vps-once-'+uuid.uuid4().hex
    call = execute_args('Imago', v['id'], command=f'printf once >> {filename}; printf remote-ok')
    result = s.mcp('exec',call)
    assert not result.get('isError'),result
    receipt = result['structuredContent']
    opid = receipt['operation_id']
    assert s.mcp('exec',call)['structuredContent']['operation_id'] == opid
    op = s.poll(opid, timeout=20)
    assert op['state'] == 'succeeded',op
    assert op['tool'] == 'exec' and op['result']['data']['exit_code'] == 0
    assert 'remote-ok' in op['output'] and '<redacted>' in op['output']
    assert (s.imago / filename).read_text() == 'once'
    assert PASSWORD not in json.dumps(op)
    saved = Store(s.hubdir)
    try:
        assert saved.one('SELECT payload FROM operations WHERE id=?',(opid,))['payload'] is None
        assert PASSWORD not in str(saved.all('SELECT * FROM operations'))
        assert PASSWORD not in str(saved.all('SELECT * FROM audit'))
    finally:saved.close()


def test_mcp_scope_and_assignments(vps_stack):
    s = vps_stack
    v = create_remote(s, project_ids=[s.projects[0]['id'],s.projects[1]['id']])
    grant = s.must(s.client.post('/api/grants',json={'label':'vps-read','scopes':['read'],'projects':[s.project['id']],'days':1}))
    listed = s.mcp('vps', {'query':v['name']}, token_value=grant['token'])
    assert not listed.get('isError'),listed
    row = listed['structuredContent']['vps'][0]
    assert row['project_ids'] == [s.project['id']]
    assert s.projects[1]['id'] not in json.dumps(listed)
    denied = s.mcp('exec',execute_args('Imago',v['id']),token_value=grant['token'])
    assert denied['isError'] and denied['structuredContent']['error']['code'] == 'INSUFFICIENT_SCOPE'
    denied = s.mcp('exec',execute_args('Lumen',v['id']))
    assert denied['isError'] and denied['structuredContent']['error']['code'] == 'VPS_NOT_FOUND'


def test_saved_ssh_remote_failure_and_timeout(vps_stack):
    s = vps_stack;v = create_remote(s)
    for command,timeout in [('exit 7',10),('sleep 5',1)]:
        result = s.mcp('exec',execute_args('Imago',v['id'],command=command,timeout_seconds=timeout))
        assert not result.get('isError'),result
        op = s.poll(result['structuredContent']['operation_id'],timeout=20)
        assert op['state'] == 'failed' and not op['result']['data']['command_ok']
        assert PASSWORD not in json.dumps(op)


def test_offline_queue_unassign_before_delivery(vps_stack):
    s = vps_stack;v = create_remote(s)
    s.stop_agent()
    try:
        call = execute_args('Imago',v['id'],command='printf should-not-run')
        result = s.mcp('exec',call)
        assert not result.get('isError'),result
        opid = result['structuredContent']['operation_id']
        s.must(s.client.put('/api/vps/'+v['id']+'/projects',json={'project_ids':[],'expected_version':v['version']}))
        op = s.poll(opid,timeout=15)
        assert op['state'] == 'failed' and op['attempts'] == 0
    finally:s.start_agent()


def test_coding_profile_actual_rpc_catalog_and_call(vps_stack):
    s = vps_stack
    headers={'Authorization':'Bearer '+s.pat,'MCP-Protocol-Version':'2025-11-25','Accept':'application/json, text/event-stream'}
    payload={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'vps','arguments':{'project':'Imago'}}}
    response=s.client.post('/mcp?profile=coding',headers=headers,json=payload)
    s.must(response)
    assert not response.json().get('error'),response.text
    assert not response.json()['result'].get('isError'),response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('change', ['disable', 'remove_reassign', 'delete'])
async def test_dispatched_revoked_request_only_probes_original_receipt(inventory, monkeypatch, accepted, change):
    """A send or ACK is not proof of no remote execution; never reconnect/replay."""
    r, owner, projects = inventory
    v = r.vps.save(body(projects[:1]), owner)
    receipt = await r.invoke('exec', execute_args(projects[0], v['id']), owner)
    operation = receipt['operation_id']
    r.store.execute('UPDATE operations SET attempts=1,accepted_at=? WHERE id=?',
                    (time.time() if accepted else None, operation))
    if change == 'disable':
        r.vps.save(edit_body(v, enabled=False), owner, v['id'])
    elif change == 'remove_reassign':
        updated = r.vps.assign(v['id'], VPSAssignments(project_ids=[], expected_version=v['version']), owner)
        r.vps.assign(v['id'], VPSAssignments(project_ids=projects[:1], expected_version=updated['version']), owner)
    else:
        r.vps.delete(v['id'], v['version'], owner)
    packets = []

    class Connection:
        journal_id = 'fixture-journal'
        unusable = False

        async def send(self, packet):
            packets.append(packet)

    def no_credential_resolution(*args, **kwargs):
        raise AssertionError('Dispatched requests must only recover the original receipt')

    project = r.project(projects[0], owner)
    r.connections[project['device_id']] = Connection()
    monkeypatch.setattr(r, 'online', lambda device_id: True)
    monkeypatch.setattr(r, 'connection_authorized', lambda device_id, connection: True)
    monkeypatch.setattr(r.vps, 'transport', no_credential_resolution)
    await r.deliver(operation)
    assert packets == [{'type': 'probe', 'id': operation}]
    row = r.store.one('SELECT state,result,attempts FROM operations WHERE id=?', (operation,))
    assert row['state'] == 'queued' and row['result'] is None and row['attempts'] == 1
