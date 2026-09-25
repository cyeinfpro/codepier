"""Real HTTP/encrypted Agent/process/desktop transport for the compact catalog."""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from tests.support import running_stack, wait_for
from shared.util import atomic_json

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.name == 'nt', reason='POSIX shell fixture')]


@pytest.fixture(scope='module')
def core_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('core-stack')) as stack:
        stack.stop_agent()
        provider = stack.directory / 'provider'
        (provider / 'bin').mkdir(parents=True)
        launcher = provider / 'bin/computer-use-client-launcher'
        launcher.write_text('#!' + sys.executable + '\nimport sys\nsys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1])) + ')\nfrom tests.fake_computer_provider import main\nmain()\n')
        launcher.chmod(0o700)
        config = json.loads(stack.config_path.read_text())
        config['shell'] = {'enabled': True, 'projects': ['Imago'], 'command': ['/bin/sh', '-c']}
        config['computer'] = {'enabled': True, 'projects': [stack.project['id']], 'allowed_apps': ['Fixture'], 'plugin_root': str(provider), 'call_timeout_seconds': 5}
        atomic_json(stack.config_path, config)
        stack.start_agent()
        stack.desktop_token = stack.must(stack.client.post('/api/grants', json={'label': 'Core desktop fixture',
            'scopes': ['read', 'computer'], 'projects': [stack.project['id']], 'days': 1}))['token']
        stack.provider = provider
        yield stack


def call(stack, name, args=None, token=None):
    arguments = {'project': 'Imago', **(args or {})} if name not in {'process'} else args
    response = stack.mcp(name, arguments, token)
    assert not response.get('isError'), response
    return response['structuredContent'], response


def finish(stack, receipt, token=None):
    result, response = call(stack, 'process', {'operation': 'wait', 'operation_ids': [receipt['operation_id']], 'wait_seconds': 5}, token)
    operation = result['operations'][0]
    assert not operation.get('pending'), operation
    assert operation['state'] == 'succeeded', operation
    return operation['result']['data'], response


def test_default_catalog_and_removed_aliases(core_stack):
    stack = core_stack
    assert {t['name'] for t in stack.rpc('tools/list').json()['result']['tools']} == {
        'workspace', 'read', 'write', 'edit', 'exec', 'process', 'vps', 'browser', 'computer'}
    for retired in ['fs_read', 'shell_exec', 'operations_get', 'browser_status', 'computer_apps', 'vps_list', 'vps_exec']:
        response = stack.mcp(retired, {'project': 'Imago'})
        assert response['isError']
    full = stack.client.post('/mcp?profile=full', headers={'Accept': 'application/json, text/event-stream',
        'Authorization': 'Bearer ' + stack.pat}, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    names = {t['name'] for t in full.json()['result']['tools']}
    assert len(names) == 9 and 'edit' in names and 'apply_patch' not in names and 'computer' in names and 'computer_action' not in names


def test_short_exec_and_files_during_another_command(core_stack):
    stack = core_stack
    long, _ = call(stack, 'exec', {'command': 'printf started; while [ ! -f release-core ]; do sleep .1; done',
        'yield_seconds': 0, 'timeout_seconds': 45, 'idempotency_key': uuid.uuid4().hex})
    try:
        wait_for(lambda: stack.client.get('/api/operations/' + long['operation_id']).json().get('output'), 5)
        args = {'command': 'printf independent', 'yield_seconds': 1, 'idempotency_key': uuid.uuid4().hex}
        short, _ = call(stack, 'exec', args)
        outcome, _ = finish(stack, short)
        assert outcome['output'] == 'independent'
        duplicate, _ = call(stack, 'exec', args)
        assert duplicate['operation_id'] == short['operation_id']
        live, _ = call(stack, 'process', {'operation': 'agent', 'project': 'Imago'})
        if live.get('pending'):
            live, _ = finish(stack, live)
        assert live['build']['runtime']['version'] and live['running_jobs'] >= 1
        diagnostics, _ = call(stack, 'process', {'operation': 'diagnostics', 'project': 'Imago'})
        assert diagnostics['tool_count'] == 9
        created, _ = call(stack, 'write', {'path': 'nested/new.txt', 'content': 'alpha\nbeta\n', 'expected_sha256': 'new', 'idempotency_key': uuid.uuid4().hex})
        read, _ = call(stack, 'read', {'path': 'nested/new.txt'})
        edited, _ = call(stack, 'edit', {'path': 'nested/new.txt', 'expected_sha256': read['sha256'],
            'edits': [{'old_text': 'alpha', 'new_text': 'beta'}, {'old_text': 'beta', 'new_text': 'alpha'}], 'idempotency_key': uuid.uuid4().hex})
        assert created['backup_id'] and edited['backup_id']
        assert (stack.imago / 'nested/new.txt').read_text() == 'beta\nalpha\n'
        status = stack.client.get('/api/operations/' + long['operation_id']).json()
        assert status['pending']
    finally:
        (stack.imago / 'release-core').touch()
        finish(stack, long)


def test_declared_parent_resource_blocks_only_overlapping_write(core_stack):
    stack = core_stack
    command, _ = call(stack, 'exec', {'command': 'printf hold; while [ ! -f release-lock ]; do sleep .1; done',
        'resources': [{'name': 'nested', 'mode': 'read'}], 'yield_seconds': 0,
        'timeout_seconds': 45, 'idempotency_key': uuid.uuid4().hex})
    try:
        wait_for(lambda: stack.client.get('/api/operations/' + command['operation_id']).json().get('output'), 5)
        # Panel dispatch has a shorter reply wait than the long command. It must
        # return a durable receipt for this conflict while other paths proceed.
        write, _ = call(stack, 'write', {'path': 'nested/blocked.txt', 'content': 'later', 'expected_sha256': 'new', 'idempotency_key': uuid.uuid4().hex})
        assert write.get('pending') and not (stack.imago / 'nested/blocked.txt').exists()
        read, _ = call(stack, 'read', {'path': 'README.md'})
        assert '# Imago' in read['content']
    finally:
        (stack.imago / 'release-lock').touch()
        finish(stack, command)
    finish(stack, write)


def test_computer_images_actions_and_browser_status(core_stack):
    stack = core_stack
    token = stack.desktop_token
    rejected = stack.mcp('computer', {'project': 'Imago', 'operation': 'apps'})
    assert rejected['isError'] and 'computer' in rejected['_meta']['mcp/www_authenticate'][0]
    opened, _ = call(stack, 'computer', {'operation': 'open', 'app': 'Fixture', 'idempotency_key': uuid.uuid4().hex}, token)
    session = opened['session_id']
    try:
        observed, image = call(stack, 'computer', {'operation': 'observe', 'session_id': session}, token)
        assert any(block['type'] == 'image' for block in image['content'])
        args = {'operation': 'action', 'session_id': session, 'observation_id': observed['observation_id'],
            'action': {'type': 'type_text', 'text': 'Fixture input'}, 'idempotency_key': uuid.uuid4().hex}
        action, _ = call(stack, 'computer', args, token)
        repeat, _ = call(stack, 'computer', args, token)
        assert repeat['operation_id'] == action['operation_id']
        assert (stack.provider / 'state.txt').read_text() == '1'
        _, polled = finish(stack, action, token)
        assert any(block['type'] == 'image' for block in polled['content'])
    finally:
        call(stack, 'computer', {'operation': 'close', 'session_id': session, 'idempotency_key': uuid.uuid4().hex}, token)
    browser, _ = call(stack, 'browser', {'operation': 'status'})
    assert browser['enabled'] is False
    refused = stack.mcp('browser', {'project': 'Imago', 'operation': 'open', 'url': 'https://example.invalid', 'idempotency_key': uuid.uuid4().hex})
    assert refused['isError']


def test_configured_task_and_nonzero_exit(core_stack):
    stack = core_stack
    receipt, _ = call(stack, 'exec', {'task': 'smoke', 'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex})
    data, _ = finish(stack, receipt)
    assert data['exit_code'] == 0 and 'PASS' in data['output']
    result = stack.mcp('exec', {'project': 'Imago', 'command': 'exit 7', 'yield_seconds': 2, 'idempotency_key': uuid.uuid4().hex})
    assert result['isError']
    assert result['structuredContent']['state'] == 'failed'


def test_advanced_workflow_review_backup_search_and_validation(core_stack):
    stack = core_stack
    help, _ = call(stack, 'workspace', {'operation': 'help', 'tool': 'process', 'action': 'validate'})
    assert help['scope'] == 'execute' and 'command' in help['inputSchema']['properties']
    opened, _ = call(stack, 'workspace', {'operation': 'open', 'capture_baseline': True})
    patched, _ = call(stack, 'edit', {'changes': [{'action': 'write', 'path': 'core-batch.txt', 'content': 'searchable-fixture\n', 'expected_sha256': 'new'}], 'idempotency_key': uuid.uuid4().hex})
    assert patched['success']
    changes, _ = call(stack, 'read', {'operation': 'changes', 'options': {'baseline_ref': opened['baseline_ref']}})
    assert any(item['path'] == 'core-batch.txt' for item in changes['files'])
    history, _ = call(stack, 'read', {'operation': 'history', 'options': {'path': 'core-batch.txt'}})
    assert history
    workflow, _ = call(stack, 'workspace', {'operation': 'workflow_create', 'options': {'title': 'Core workflow', 'goal': 'Validate the fixture'}, 'idempotency_key': uuid.uuid4().hex})
    detail, _ = call(stack, 'workspace', {'operation': 'workflow_get', 'options': {'workflow_id': workflow['workflow_id']}})
    assert detail['workflow_id'] == workflow['workflow_id']
    artifact, _ = call(stack, 'write', {'operation': 'artifact', 'options': {'path': 'core-batch.txt'}, 'idempotency_key': uuid.uuid4().hex})
    listed, _ = call(stack, 'read', {'operation': 'artifacts'})
    assert artifact['artifact_id'] in json.dumps(listed)
    search, _ = call(stack, 'process', {'operation': 'search_start', 'project': 'Imago', 'options': {'query': 'searchable-fixture'}, 'idempotency_key': uuid.uuid4().hex})
    search_data, _ = finish(stack, search)
    assert search_data['search_id']
    validation, _ = call(stack, 'process', {'operation': 'validate', 'project': 'Imago', 'options': {'command': 'printf validation-ok'}, 'idempotency_key': uuid.uuid4().hex})
    result, _ = finish(stack, validation)
    assert result['exit_code'] == 0
    validations, _ = call(stack, 'process', {'operation': 'validation_list', 'project': 'Imago'})
    assert validations
