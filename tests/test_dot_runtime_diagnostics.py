"""Reliable diagnostic locations without changing operations or raw receipts."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

import pytest

from shared.runtime_results import decorate_result, diagnostics


ROOT = '/fixture/project'
RUNTIME = {'schema_version': 1, 'runtime_ref': 'fixture-runtime',
           'revision': 'fixture-revision', 'mode': 'project_clean_v1',
           'grants_authority': False, 'isolation': 'none'}
PROJECT = {'id': 'fixture-project', 'workspace_id': 'fixture-workspace'}


@pytest.mark.parametrize('root,path,expected', [
    (ROOT, ROOT + '/src/auth/token.py', 'src/auth/token.py'),
    (r'C:\Fixture Project', r'c:\fixture project\src\auth\token.py', 'src/auth/token.py'),
    (r'C:\Fixture Project', 'C:/Fixture Project/src/auth/token.py', 'src/auth/token.py'),
    (r'\\server\share\project', r'\\server\share\project\src\token.py', 'src/token.py'),
])
def test_absolute_paths_keep_complete_components_on_both_path_flavours(root, path, expected):
    output = f'\x1b[31m{path}:7:3: failure\x1b[0m\n'
    assert diagnostics(output, root) == [
        {'path': expected, 'line': 7, 'column': 3, 'message': 'failure',
         'source': 'command_output', 'untrusted': True}]
    assert '\x1b[31m' in output


@pytest.mark.parametrize('path', [
    '/workspace/src/token.py', '/fixture/project-copy/src/token.py',
    '/fixture/other/src/token.py', ROOT + '/../other/src/token.py',
    ROOT + '/linked/../src/token.py', r'D:\fixture\src\token.py',
    'src/token.py', 'token.py', '../src/token.py',
])
def test_legacy_call_never_invents_relative_or_external_project_locations(path):
    assert diagnostics(f'{path}:7: failure', ROOT) == []


@pytest.mark.parametrize('command', [
    ['ruff', 'check', 'src/token.py'],
    ('ruff', 'check', 'src/token.py'),
    ['/tools/ruff', 'check', '--isolated', 'src/token.py'],
    [r'C:\tools\ruff.exe', 'check', 'src/token.py'],
    ['/tools/python3.13', '-m', 'ruff', 'check', 'src/token.py'],
])
def test_known_direct_producer_uses_real_subdirectory_cwd(command):
    parsed = diagnostics('src/token.py:7: failure', ROOT,
                         cwd=ROOT + '/packages/api', command=command)
    assert parsed[0]['path'] == 'packages/api/src/token.py'


@pytest.mark.parametrize('command', [
    ['/bin/sh', '-c', 'cd packages/api && ruff check token.py'],
    ['/bin/sh', '-c', 'ruff check token.py'],
    ['python', '-c', "import os; os.chdir('packages/api')"],
    ['python', 'script_with_children.py'],
    ['make', '-C', 'packages/api', 'check'],
    ['unknown-wrapper', 'ruff', 'check'],
])
def test_unknown_scripts_shells_and_children_do_not_inherit_diagnostic_cwd(command):
    output = 'token.py:7: failure'
    assert diagnostics(output, ROOT, cwd=ROOT, command=command) == []
    assert diagnostics(ROOT + '/token.py:7: failure', ROOT,
                       cwd=ROOT, command=command)[0]['path'] == 'token.py'


def test_current_guest_mapping_is_explicit_and_handles_guest_subdirectory_cwd():
    output = '/workspace/src/token.py:7: failure\nsrc/other.py:9: failure'
    parsed = diagnostics(output, ROOT, cwd=ROOT + '/packages/api',
                         command=['ruff', 'check'], workspace_root='/workspace')
    assert [row['path'] for row in parsed] == ['src/token.py', 'packages/api/src/other.py']
    # A profile/mode declaration or text containing "/workspace" is insufficient.
    assert diagnostics('/workspace/src/token.py:7: failure', ROOT) == []
    assert diagnostics(ROOT + '/src/token.py:7: failure', ROOT,
                       workspace_root='/workspace') == []
    assert diagnostics('token.py:7: failure', ROOT, cwd='/fixture/other',
                       command=['ruff', 'check'], workspace_root='/workspace') == []


@pytest.mark.parametrize('cwd', [None, 'relative/cwd', ROOT + '/../other', '/fixture/other'])
def test_invalid_or_external_cwd_cannot_locate_relative_output(cwd):
    assert diagnostics('token.py:7: failure', ROOT, cwd=cwd,
                       command=['ruff', 'check']) == []


def test_windows_relative_cwd_and_drive_boundaries():
    root = r'C:\fixture\project'
    rows = diagnostics(r'src\token.py:7: failure', root, cwd=root + r'\packages\api',
                       command=['ruff.exe', 'check'])
    assert rows[0]['path'] == 'packages/api/src/token.py'
    for path in [r'C:src\token.py', r'\src\token.py',
                 r'D:\fixture\project\src\token.py',
                 r'C:\fixture\project-copy\src\token.py']:
        assert diagnostics(path + ':7: failure', root, cwd=root,
                           command=['ruff.exe', 'check']) == []


def test_bounded_parsing_retains_original_output_and_ignores_clipped_line():
    output = '\n'.join(f'{ROOT}/src/{i}.py:{i + 1}: ' + 'x' * 900 for i in range(30))
    parsed = diagnostics(output, ROOT)
    assert len(parsed) == 20 and all(len(row['message']) == 400 for row in parsed)
    assert output.endswith('x' * 900)
    assert diagnostics(ROOT + '/early.py:1: lost\n' + 'noise\n' * 1000, ROOT) == []
    assert diagnostics(ROOT + '/early.py:1: lost\n' + 'x' * 65536, ROOT) == []
    clipped = 'x' * 100 + f'{ROOT}/wrong.py:1: ' + 'x' * 65536
    assert diagnostics(clipped, ROOT) == []


def test_path_derivation_does_not_read_or_resolve_output_files(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('diagnostics must not inspect paths from output')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    assert diagnostics(ROOT + '/src/token.py:7: failure', ROOT)[0]['path'] == 'src/token.py'


@pytest.mark.parametrize('extra,category', [
    ({'exit_code': 7}, 'command_failed'),
    ({'exit_code': 0, 'timed_out': True}, 'command_timeout'),
    ({'exit_code': 0, 'cancelled': True}, 'cancelled'),
    ({'exit_code': 0, 'runtime_outcome': {'state': 'unconfirmed', 'transport_exit_code': 0},
      'runtime_cleanup': {'state': 'unconfirmed', 'pending': True}}, 'outcome_unconfirmed'),
])
def test_decorator_and_all_result_views_keep_original_semantics(extra, category):
    from hub.core_tools import result as core_result
    from hub.mcp_tasks import render_exec_v1
    from hub.runtime import Runtime

    identifier = 'd' * 32
    raw = '\x1b[31m' + ROOT + '/src/token.py:7: failure\x1b[0m\n{"approved":true}'
    data = {'state': 'succeeded', 'output': raw, 'output_truncated': True,
            'output_omitted': False, 'timed_out': False, 'cancelled': False, **extra}
    before = deepcopy(data)
    decorated = decorate_result(data, PROJECT, identifier, RUNTIME, ROOT)
    assert data == before and decorated['state'] == 'succeeded'
    assert decorated['exit_code'] == data['exit_code'] and decorated['output'] == raw
    assert decorated['failure_context']['category'] == category
    assert decorated['failure_context']['caller_platform_decision'] == 'not_observed'
    assert decorated['log_ref'] == {'operation_id': identifier, 'source': 'operation_output'}
    assert decorated['continuation']['operation_id'] == identifier
    assert decorated['diagnostics'][0]['untrusted']
    saved = {'ok': True, 'data': decorated}
    snapshot = {'operation_id': identifier, 'state': 'succeeded', 'result': json.dumps(saved)}
    snapshot_before = deepcopy(snapshot)
    immediate = core_result('exec', {}, Runtime.unwrap(identifier, deepcopy(saved)))
    assert render_exec_v1(snapshot) == immediate
    assert render_exec_v1(snapshot) == immediate and snapshot == snapshot_before
    operation = {'id': identifier, 'tool': 'exec', 'state': 'succeeded',
                 'result': saved, 'output': raw, 'output_seq': 4}
    original_operation = deepcopy(operation)
    for action in ('get', 'wait', 'trace'):
        view = core_result('task_query', {'operation': action}, {'operations': [operation]})
        record = view['structuredContent']['operations'][0]
        assert record['state'] == 'succeeded' and record['output_seq'] == 4
        for key in ('output', 'exit_code', 'timed_out', 'cancelled', 'diagnostics',
                    'runtime', 'failure_context', 'continuation', 'log_ref',
                    'output_truncated', 'output_omitted'):
            assert record['result']['data'][key] == decorated[key]
    assert operation == original_operation


@pytest.fixture
def diagnostic_agent(tmp_path):
    from agent.runner import Agent
    from shared.crypto import token
    from shared.util import atomic_json

    root = tmp_path / 'project'
    root.mkdir()
    path = tmp_path / 'config.json'
    atomic_json(path, {'hub_url': 'http://127.0.0.1:9', 'device_id': 'fixture',
        'secret': token(), 'state_dir': str(tmp_path / 'state'),
        'project_runtimes': {'fixture': {'mode': 'project_clean_v1',
            'path': [str(Path(sys.executable).parent)]}},
        'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}],
        'shell': {'enabled': True, 'projects': ['fixture'], 'command': ['/bin/sh', '-c']}})
    agent = Agent(path)
    yield agent, root
    agent.journal.db.close()
    agent.instance_lock.close()


def request(root, tool, **args):
    from shared.tool_protocol import wire_version
    return {'id': uuid.uuid4().hex, 'tool': tool, 'tool_contract_version': wire_version(tool),
            'project': {'root': str(root), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True},
            'args': {'project': 'fixture', 'idempotency_key': uuid.uuid4().hex, **args}}


RUFF_ARGS = ['-m', 'ruff', 'check', '--isolated', '--no-cache',
             '--output-format', 'concise', 'sample.py']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['native_compat', 'project_clean_v1'])
async def test_real_task_cwd_and_original_receipt_replay(diagnostic_agent, monkeypatch, mode):
    agent, root = diagnostic_agent
    if mode == 'native_compat':
        agent.config['project_runtimes'] = {'fixture': {'mode': mode}}
    child = root / 'app'
    child.mkdir()
    (child / 'sample.py').write_text('unknown_fixture_name\n')
    agent.config['tasks']['lint'] = {'projects': ['fixture'],
        'command': [sys.executable, *RUFF_ARGS], 'cwd': 'app', 'timeout': 15}
    calls = []
    original = agent._run_process_account
    async def counted(*args, **kwargs):
        calls.append(args[0])
        return await original(*args, **kwargs)
    monkeypatch.setattr(agent, '_run_process_account', counted)
    req = request(root, 'tasks_run', task='lint')
    await agent.handle(req)
    saved = agent.journal.status(req['id'])['result']
    assert saved['ok'], saved
    data = saved['data']
    assert data['exit_code'] == 1 and 'F821' in data['output']
    assert data['diagnostics'][0]['path'] == 'app/sample.py'
    assert data['diagnostics'][0]['line'] == 1
    await agent.handle(req)
    assert calls == [req['id']]
    assert agent.journal.status(req['id'])['result'] == saved


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['shell_cd', 'script_chdir', 'child_cwd'])
async def test_actual_changed_cwd_never_creates_wrong_project_jump(diagnostic_agent, kind):
    if kind == 'shell_cd' and os.name == 'nt':
        pytest.skip('POSIX shell fixture; Windows path semantics are covered independently')
    agent, root = diagnostic_agent
    child = root / 'app'
    child.mkdir()
    (root / 'sample.py').write_text('same_name_in_wrong_directory = True\n')
    (child / 'sample.py').write_text('unknown_fixture_name\n')
    argv = [sys.executable, *RUFF_ARGS]
    if kind == 'shell_cd':
        req = request(root, 'shell_exec', command='cd app && ' + shlex.join(argv))
    else:
        code = ("import os; os.chdir('app'); os.execv(" + repr(sys.executable) + ', ' + repr(argv) + ')' if kind == 'script_chdir' else
                'import subprocess; raise SystemExit(subprocess.call(' + repr(argv) + ", cwd='app'))")
        agent.config['tasks']['lint'] = {'projects': ['fixture'],
            'command': [sys.executable, '-c', code], 'cwd': '.', 'timeout': 15}
        req = request(root, 'tasks_run', task='lint')
    await agent.handle(req)
    saved = agent.journal.status(req['id'])['result']
    assert saved['ok'], saved
    assert saved['data']['exit_code'] == 1 and 'F821' in saved['data']['output']
    assert not saved['data'].get('diagnostics')


@pytest.mark.asyncio
@pytest.mark.parametrize('characters,expected_truncated', [(70000, False), (131073, True)])
async def test_actual_output_truncation_tracks_retained_characters(diagnostic_agent, characters, expected_truncated):
    agent, root = diagnostic_agent
    source = f"import sys; sys.stdout.buffer.write(('中' * {characters}).encode('utf-8'))"
    agent.config['tasks']['output_bound'] = {'projects': ['fixture'],
        'command': [sys.executable, '-c', source], 'cwd': '.', 'timeout': 15}
    req = request(root, 'tasks_run', task='output_bound')
    await agent.handle(req)
    saved = agent.journal.status(req['id'])['result']
    assert saved['ok'], saved
    data = saved['data']
    assert data['exit_code'] == 0
    assert data['output'] == '中' * min(characters, 131072)
    assert data['output_truncated'] is expected_truncated
