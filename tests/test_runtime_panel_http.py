"""Actual authenticated panel → durable operation → Agent config/reload round trips."""
import json
import uuid
from tests.support import wait_for


def endpoint(stack, project=None):
    return '/api/projects/' + (project or stack.project['id']) + '/runtime'


def snapshot(stack, project=None):
    value = stack.must(stack.client.get(endpoint(stack, project)))
    if value.get('pending'):
        row = stack.poll(value['operation_id'], timeout=25)
        assert row['state'] == 'succeeded', row
        value = row['result']['data']
    return value


def applied(stack, mode, project=None):
    def check():
        value = snapshot(stack, project)
        return value if value.get('state') == 'applied' and value['profile']['mode'] == mode else None
    return wait_for(check, timeout=40)


def set_mode(stack, mode, snapshot_value=None, project=None, **fields):
    before = snapshot_value or snapshot(stack, project)
    body = {'expected_revision': before['revision'], 'profile': {'mode': mode, **fields},
            'idempotency_key': 'runtime-http-' + uuid.uuid4().hex}
    response = stack.client.put(endpoint(stack, project), json=body)
    return response, body


def test_panel_reads_actual_default_and_switches_without_editing_other_settings(stack):
    before = json.loads(stack.config_path.read_text())
    first = snapshot(stack)
    assert first['default_mode'] == first['profile']['mode'] == 'project_clean_v1'
    assert first['selection_source'] == 'default' and first['state'] == 'applied'
    assert first['profile']['credentials'] == {
        'github_cli': True, 'git_config': True, 'ssh_agent': True}
    assert first['clean_default_profile']['credentials'] == first['profile']['credentials']
    response, body = set_mode(stack, 'native_compat', first)
    result = stack.must(response)
    original_id = result['operation_id']
    done = stack.poll(original_id, timeout=25)
    assert done['state'] == 'succeeded', done
    ready = applied(stack, 'native_compat')
    assert ready['runtime']['mode'] == 'native_compat'
    raw = json.loads(stack.config_path.read_text())
    config = raw.pop('project_runtimes')
    assert raw == before and config[stack.project['id']]['mode'] == 'native_compat'
    receipt = stack.must(stack.client.get(endpoint(stack) + '/receipts/' + body['idempotency_key']))
    assert receipt['operation_id'] == original_id and receipt['outcome_confirmed']
    replay = stack.must(stack.client.put(endpoint(stack), json=body))
    assert replay['operation_id'] == original_id
    response, _ = set_mode(stack, 'project_clean_v1', ready)
    stack.must(response); applied(stack, 'project_clean_v1')


def test_original_save_conflict_retains_newer_settings(stack):
    old = snapshot(stack)
    response, _ = set_mode(stack, 'project_clean_v1', old, path=['one'])
    stack.must(response); applied(stack, 'project_clean_v1')
    result, body = set_mode(stack, 'native_compat', old)
    if result.is_success:
        data = result.json(); row = stack.poll(data['operation_id'], timeout=25)
        assert row['state'] == 'failed'
        assert row['result']['error']['code'] == 'SETTINGS_CHANGED'
    else:
        assert result.json()['error']['code'] == 'SETTINGS_CHANGED'
    assert snapshot(stack)['profile']['path'] == ['one']


def test_settings_are_panel_only_and_csrf_protected(stack):
    before = snapshot(stack)
    body = {'expected_revision': before['revision'], 'profile': {'mode': 'native_compat'}, 'idempotency_key': uuid.uuid4().hex}
    result = stack.client.put(endpoint(stack), json=body, headers={'X-RD-CSRF': 'wrong'})
    assert result.status_code == 403
    for name in ('project_runtime_get', 'project_runtime_set'):
        arguments = {'project': stack.project['id'], **(body if name.endswith('set') else {})}
        denial = stack.mcp(name, arguments)
        assert denial['isError']
    catalog = stack.rpc('tools/list').json()['result']['tools']
    assert not any(row['name'] in {'project_runtime_get','project_runtime_set'} for row in catalog)


def test_receipts_cannot_be_used_for_another_project_and_other_projects_keep_default(stack):
    second = stack.projects[1]['id']
    assert snapshot(stack, second)['profile']['mode'] == 'project_clean_v1'
    response, body = set_mode(stack, 'native_compat')
    value = stack.must(response); stack.poll(value['operation_id'], timeout=25); applied(stack, 'native_compat')
    other = stack.must(stack.client.get(endpoint(stack, second) + '/receipts/' + body['idempotency_key']))
    assert other['state'] == 'not_found' and not other['outcome_confirmed']
    assert snapshot(stack, second)['selection_source'] == 'default'
    response, _ = set_mode(stack, 'project_clean_v1'); stack.must(response); applied(stack, 'project_clean_v1')


def test_offline_read_never_presents_intended_default_as_loaded_state(stack):
    stack.stop_agent()
    try:
        wait_for(lambda: snapshot(stack).get('state') == 'offline', timeout=20)
        data = snapshot(stack)
        assert data['profile'] is None and data['runtime'] is None and not data['can_save']
        response = stack.client.put(endpoint(stack), json={'expected_revision':'a'*64,
            'profile':{'mode':'native_compat'}, 'idempotency_key':uuid.uuid4().hex})
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'AGENT_OFFLINE'
    finally:
        stack.start_agent()


def test_configuration_changes_do_not_restart_an_existing_command(stack):
    from shared.util import atomic_json
    # All configuration and execution below belong to the disposable fixture.
    stack.stop_agent()
    raw = json.loads(stack.config_path.read_text())
    raw['shell'] = {'enabled': True, 'projects': ['ProjectAlpha'], 'command': ['/bin/sh', '-c']}
    raw.setdefault('project_runtimes', {})[stack.project['id']] = {'mode': 'project_clean_v1'}
    atomic_json(stack.config_path, raw); stack.start_agent()
    start = stack.mcp('exec', {'project': 'ProjectAlpha', 'command': 'printf once >> runtime-count; printf running; sleep 3; printf done',
        'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex})['structuredContent']
    identifier = start['operation_id']
    wait_for(lambda: 'running' in stack.client.get('/api/operations/' + identifier).json().get('output', ''), timeout=15)
    response, _ = set_mode(stack, 'native_compat')
    stack.must(response)
    row = stack.poll(identifier, timeout=40)
    assert row['state'] == 'succeeded', row
    assert row['result']['data']['runtime']['mode'] == 'project_clean_v1'
    assert (stack.projectalpha / 'runtime-count').read_text() == 'once'
    applied(stack, 'native_compat')
    after = stack.mcp('exec', {'project': 'ProjectAlpha', 'command': 'printf new-command',
        'idempotency_key': uuid.uuid4().hex})['structuredContent']
    row = stack.poll(after['operation_id'], timeout=20)
    assert row['result']['data']['runtime']['mode'] == 'native_compat'


def test_readonly_project_can_change_runtime_without_enabling_execution(stack):
    original = {k: stack.project[k] for k in ('alias','root','device_id','description','mode','allow_tasks')}
    try:
        stack.must(stack.client.put('/api/projects/' + stack.project['id'], json={**original, 'mode': 'read', 'allow_tasks': False}))
        response, _ = set_mode(stack, 'project_clean_v1')
        value = stack.must(response); row = stack.poll(value['operation_id'], timeout=30)
        assert row['state'] == 'succeeded', row
        applied(stack, 'project_clean_v1')
        projects = stack.client.get('/api/projects').json()['projects']
        saved = next(p for p in projects if p['id'] == stack.project['id'])
        assert saved['mode'] == 'read' and saved['allow_tasks'] is False
    finally:
        stack.must(stack.client.put('/api/projects/' + stack.project['id'], json=original))


def test_clean_credentials_http_roundtrip_uses_defaults_and_preserves_explicit_opt_outs(stack):
    # Only the disposable Agent's settings are changed. No GitHub request,
    # native credential helper or SSH operation is executed by this journey.
    keys = ('github_cli', 'git_config', 'ssh_agent')
    other = stack.projects[1]['id']
    other_before = snapshot(stack, other)['profile']
    on, off = dict.fromkeys(keys, True), dict.fromkeys(keys, False)
    for fields, choices in [
        ({'credentials': on}, on),
        ({'credentials': off}, off),
        ({'path': ['credential-tools']}, off),
        ({'credentials': {'github_cli': False}}, {**on, 'github_cli': False}),
        ({'credentials': {}}, on),
    ]:
        response, body = set_mode(stack, 'project_clean_v1', **fields)
        submitted = stack.must(response)
        completed = stack.poll(submitted['operation_id'], timeout=30)
        assert completed['state'] == 'succeeded', completed
        ready = applied(stack, 'project_clean_v1')
        assert ready['profile']['credentials'] == choices
        assert ready['runtime']['mode'] == 'project_clean_v1'
        assert json.loads(stack.config_path.read_text())['project_runtimes'][stack.project['id']]['credentials'] == choices
        for key in keys:
            enabled = choices[key]
            source = ready['readiness']['credentials'][key]
            assert source['enabled'] is enabled
            if enabled:
                assert source['state'] in {'configured', 'missing'}
            else:
                assert source['state'] == 'disabled' and source['source'] == 'none'
        receipt = stack.must(stack.client.get(endpoint(stack) + '/receipts/' + body['idempotency_key']))
        assert receipt['operation_id'] == submitted['operation_id']
        assert receipt['outcome_confirmed']
    assert snapshot(stack, other)['profile'] == other_before


def test_clean_credentials_http_rejects_explicit_null_without_writing(stack):
    before = snapshot(stack)
    raw = stack.config_path.read_bytes()
    response, _ = set_mode(stack, 'project_clean_v1', before, credentials=None)
    assert response.status_code in {400, 422}, response.text
    assert response.json().get('error'), response.text
    assert stack.config_path.read_bytes() == raw
    assert snapshot(stack)['revision'] == before['revision']
