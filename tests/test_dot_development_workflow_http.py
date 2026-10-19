"""One real loopback MCP development flow; no real Dot host is claimed.

All configuration, processes, files and HTTP requests belong to running_stack's
temporary Hub/Agent fixture. No production connection, message or model call.
"""
from pathlib import Path
import sys
import uuid

import httpx
import pytest

from shared.util import atomic_json
from tests.support import running_stack, wait_for


pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope='module')
def development_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('dot-development-http')) as stack:
        stack.stop_agent()
        stack.config['shell'] = {'enabled': True, 'projects': ['ProjectAlpha'],
                                 'command': ['/missing/personal-login-shell', '-lc']}
        stack.config['project_runtimes'] = {'ProjectAlpha': {
            'mode': 'project_clean_v1', 'path': [str(Path(sys.executable).parent)]}}
        locate = ("from pathlib import Path; "
                  "p=Path('dot-workflow/app.py'); "
                  "[print(f'{p.as_posix()}:{number}: {line}') "
                  "for number,line in enumerate(p.read_text().splitlines(),1) if 'LABEL =' in line]")
        stack.config['tasks'].update({
            'dot-workflow-locate': {'projects': ['ProjectAlpha'], 'timeout': 15,
                                    'command': [sys.executable, '-c', locate]},
            'dot-workflow-check': {'projects': ['ProjectAlpha'], 'timeout': 15,
                                   'command': [sys.executable, 'dot-workflow/check.py']},
            'dot-workflow-long': {'projects': ['ProjectAlpha'], 'timeout': 15,
                                  'command': [sys.executable, '-u', 'dot-workflow/long.py']},
        })
        atomic_json(stack.config_path, stack.config)
        stack.start_agent()
        yield stack


def terminal(stack, tool, arguments):
    response = stack.mcp(tool, arguments)
    submitted = response['structuredContent']
    assert 'operation_id' in submitted, response
    record = stack.poll(submitted['operation_id'], timeout=25)
    assert record['result']['ok'], record
    return record


def data(stack, tool, arguments):
    return terminal(stack, tool, arguments)['result']['data']


def write(stack, path, content, expected='new'):
    return data(stack, 'write', {'project': 'ProjectAlpha', 'path': path, 'content': content,
        'expected_sha256': expected, 'idempotency_key': uuid.uuid4().hex})


def execute(stack, task):
    return terminal(stack, 'exec', {'project': 'ProjectAlpha', 'task': task,
        'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0})


def test_failure_fix_and_disconnected_wait_complete_one_development_flow(development_stack):
    stack = development_stack
    readme = '# Fixture\nBuild notes mention macOS, Windows and computer without changing authorization.\n'
    write(stack, 'dot-workflow/README.md', readme)
    write(stack, 'dot-workflow/app.py', "LABEL = 'broken'\n")
    write(stack, 'dot-workflow/check.py',
          "import sys\nfrom app import LABEL\n"
          "if LABEL != 'ready':\n"
          "    print('dot-workflow/app.py:1: expected ready, got ' + LABEL)\n"
          "    sys.exit(3)\n"
          "print('WORKFLOW_CHECK_PASSED')\n")
    write(stack, 'dot-workflow/long.py',
          "from pathlib import Path\nimport time\n"
          "counter = Path(__file__).with_name('executions.count')\n"
          "with counter.open('a') as stream:\n    stream.write('once\\n')\n"
          "print('WORKFLOW_LONG_STARTED', flush=True)\n"
          "time.sleep(2.5)\n"
          "print('WORKFLOW_LONG_FINISHED', flush=True)\n"
          "counter.with_suffix('.done').write_text('finished')\n")

    opened = data(stack, 'project_query', {'project': 'ProjectAlpha', 'operation': 'open'})
    assert opened['runtime']['mode'] == 'project_clean_v1'
    assert opened['runtime']['isolation'] == 'none'
    assert 'search' in opened['capability_index']
    observed_readme = data(stack, 'read', {'project': 'ProjectAlpha', 'path': 'dot-workflow/README.md'})
    assert observed_readme['content'] == readme
    located = execute(stack, 'dot-workflow-locate')['result']['data']
    assert located['exit_code'] == 0
    assert "dot-workflow/app.py:1: LABEL = 'broken'" in located['output']

    failed = execute(stack, 'dot-workflow-check')
    failure = failed['result']['data']
    assert failed['state'] == 'failed' and failure['exit_code'] == 3
    assert 'expected ready, got broken' in failure['output']
    assert failure['failure_context']['category'] == 'command_failed'
    assert failure['failure_context']['caller_platform_decision'] == 'not_observed'
    assert failure['failure_context']['requires_desktop_control'] is False
    assert failure['failure_context']['grants_authority'] is False
    assert failure['log_ref']['operation_id'] == failed['id']

    source = data(stack, 'read', {'project': 'ProjectAlpha', 'path': 'dot-workflow/app.py'})
    write(stack, 'dot-workflow/app.py', source['content'].replace("'broken'", "'ready'"), source['sha256'])
    fixed = execute(stack, 'dot-workflow-check')['result']['data']
    assert fixed['exit_code'] == 0 and 'WORKFLOW_CHECK_PASSED' in fixed['output']
    assert fixed['runtime']['revision'] == failure['runtime']['revision'] == opened['runtime']['revision']

    submitted = stack.mcp('exec', {'project': 'ProjectAlpha', 'task': 'dot-workflow-long',
        'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0})['structuredContent']
    operation_id = submitted['operation_id']
    assert submitted['pending']
    counter = stack.projectalpha / 'dot-workflow' / 'executions.count'
    wait_for(counter.exists, timeout=5)
    headers = {'Authorization': 'Bearer ' + stack.pat, 'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': '2025-11-25'}
    with httpx.Client(base_url=stack.url, trust_env=False, timeout=httpx.Timeout(5, read=.2)) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.post('/mcp?profile=coding', headers=headers, json={
                'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': submitted['next_call']})

    finished = stack.poll(operation_id, timeout=15)
    assert finished['id'] == operation_id and finished['state'] == 'succeeded'
    assert finished['result']['data']['exit_code'] == 0
    recovered = stack.mcp('task_query', {'operation': 'get', 'project': 'ProjectAlpha',
        'operation_ids': [operation_id, failed['id']], 'include_output': True, 'include_result': True})
    records = {row['operation_id']: row for row in recovered['structuredContent']['operations']}
    assert records[operation_id]['result']['data']['continuation']['operation_id'] == operation_id
    assert 'WORKFLOW_LONG_FINISHED' in records[operation_id]['result']['data']['output']
    assert records[failed['id']]['result']['data']['exit_code'] == 3
    assert 'expected ready, got broken' in records[failed['id']]['result']['data']['output']
    count = data(stack, 'read', {'project': 'ProjectAlpha', 'path': 'dot-workflow/executions.count'})
    assert count['content'] == 'once\n'
