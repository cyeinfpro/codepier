"""No real Codex invocation: static inspection, blocked-spawn traps and fake stacks."""
from __future__ import annotations
import asyncio
import copy
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from shared.execution_policy import InvocationInspector, agent_blocks_codex, validate_policy, enforce_argv
from shared.contracts import ShellExec
from shared.util import DevError, atomic_json
from hub.runtime import Principal, remote_codex_denial
from tests.test_shell import shell_agent, shell_request
from tests.test_audit_runtime import runtime, Socket, until


BLOCKED = [
    'codex exec task', '/opt/homebrew/bin/codex --version', 'CODEX.EXE exec task',
    '"/Applications/My Tools/codex" exec task', "'co''dex' exec task", r'co\dex exec task',
    "env A=1 codex exec task", 'sudo -u owner -- codex exec task',
    'nohup codex exec task &', 'command codex -v', 'exec codex exec task',
    'nice -n 10 codex exec task', 'printf x | codex exec task',
    'true && codex exec task', 'false || codex exec task',
    'echo "$(codex exec task)"', 'printf "%s" `codex --version`',
    "bash -lc 'codex exec task'", "zsh -c 'env A=1 codex exec task'",
    "eval 'codex exec task'", "C=codex; \"$C\" exec task",
    "A='co'; B='dex'; ${A}${B} exec task", 'export C=codex; $C exec task',
    'npx --yes @openai/codex exec task', 'npm exec --package=@openai/codex -- codex exec task',
    'pnpm dlx @openai/codex@1.0.0 exec task', 'yarn dlx @openai/codex exec task',
    "find . -exec codex '{}' ';'", 'xargs -I {} codex exec {}',
    "python3 -c 'import subprocess; subprocess.run([\"codex\", \"exec\", \"task\"])'",
    "python3 -c 'from subprocess import run as launch; launch([\"codex\"])'",
    "python3 -c 'import os; os.system(\"codex exec task\")'",
    "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['codex'])\nPY\n",
    "bash <<'SH'\ncodex exec task\nSH\n",
    "node -e 'require(\"child_process\").spawn(\"codex\", [])'",
    "node -e 'require(\"child_process\").execSync(\"codex exec task\")'",
    "node -e 'require(\"@openai/codex-sdk\")'",
    "& 'C:\\Tools\\codex.exe' exec task", 'Start-Process codex.exe',
    'open -a Codex', 'open -b com.openai.codex',
]
ALLOWED = [
    'printf ordinary-shell', 'git status --short', 'git diff -- agent/shell.py',
    'grep -R "codex" agent hub', 'rg -n "codex|shell_exec" agent hub',
    'echo codex', "printf '%s' 'codex exec task'", 'cat docs/CODEX-SKILLS.md',
    "git commit -m 'fix codex command policy'", 'git checkout codex-feature',
    "find . -name '*codex*'", 'ls /tmp/codex', 'command -v codex',
    'which codex', 'type codex', 'npm view @openai/codex version',
    'npm install @openai/codex', 'npm test', 'pnpm build', 'pi --version',
    '.venv/bin/python -m pytest tests/test_codex_skills.py',
    "cat <<'EOF' > example.sh\ncodex exec task\nEOF\n",
    "python3 -c 'print(\"codex\")'",
    "python3 - <<'PY'\nfrom pathlib import Path\nPath('x.py').write_text(\"import subprocess; subprocess.run(['codex'])\")\nPY\n",
    "node -e 'console.log(\"codex\")'", "# codex exec task\nprintf ok",
    "A=codex; printf '%s' \"$A\"", 'echo "literal $(printf codex)"',
    "curl -I https://example.invalid/codex", 'ssh server uname -a',
]


@pytest.mark.parametrize('command', BLOCKED)
def test_executable_positions_and_known_wrappers_are_blocked(command):
    assert InvocationInspector().shell(command), command


@pytest.mark.parametrize('command', ALLOWED)
def test_mentions_reading_writing_and_unrelated_commands_are_preserved(command):
    assert not InvocationInspector().shell(command), command


def test_requested_environment_expansion_is_inspected():
    assert InvocationInspector(env={'RUNNER': 'codex'}).shell('$RUNNER exec task')
    assert not InvocationInspector(env={'RUNNER': 'codex'}).shell('printf "%s" "$RUNNER"')


@pytest.mark.parametrize('value', [None, [], True, {'typo': True}, {'block_local_codex': 'true'}, {'block_local_codex': 1}])
def test_local_policy_configuration_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        validate_policy(value)


@pytest.mark.parametrize('policy', [True, [], {}, {'version': True, 'origin':'panel', 'block_local_codex':False}, {'version':1, 'origin':[], 'block_local_codex':False}])
def test_malformed_delivery_metadata_does_not_grant_exemption(policy):
    assert agent_blocks_codex({}, {'_execution_policy': policy})


def policy(origin='mcp', blocked=True):
    return {'version': 1, 'origin': origin, 'block_local_codex': blocked}


def test_local_floor_legacy_hub_and_authenticated_panel_are_distinct(tmp_path):
    config = {'mcp_policy': {'block_local_codex': True}}
    assert agent_blocks_codex(config, {})
    assert agent_blocks_codex(config, {'_execution_policy': policy(blocked=False)})
    assert not agent_blocks_codex(config, {'_execution_policy': policy('panel', False)})
    assert not agent_blocks_codex({}, {})
    enforce_argv(config, {'_execution_policy': policy('panel', False)}, ['codex'], tmp_path, {})


@pytest.mark.parametrize('extra', [{'execution_policy': policy('panel', False)}, {'admin': True}, {'origin': 'panel'}])
def test_tool_arguments_cannot_supply_authenticated_exemption(extra):
    with pytest.raises(ValidationError):
        ShellExec(project='fixture', command='echo ok', idempotency_key='policy-spoof-check', **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['shell', 'task', 'python_script', 'shell_script', 'symlink', 'env', 'legacy', 'spoofed_project'])
async def test_agent_rejects_before_any_process_creation(shell_agent, monkeypatch, kind):
    agent, root = shell_agent
    agent.config['mcp_policy'] = {'block_local_codex': True}
    req = shell_request(root, 'codex exec test-only')
    req['execution_policy'] = policy()
    if kind == 'task':
        agent.config['tasks']['guarded'] = {'projects':['fixture'], 'command':['codex', 'exec', 'test-only']}
        req['tool'] = 'tasks_run'
        req['args'] = {'project':'fixture', 'task':'guarded', 'idempotency_key':uuid.uuid4().hex}
    elif kind == 'python_script':
        (root/'helper.py').write_text("import subprocess\nsubprocess.run(['codex'])\n")
        req['args']['command'] = 'python3 helper.py'
    elif kind == 'shell_script':
        (root/'helper.sh').write_text('#!/bin/sh\ncodex exec task\n')
        req['args']['command'] = 'sh helper.sh'
    elif kind == 'symlink':
        (root/'codex').write_text('#!/bin/sh\ntouch never-created\n')
        (root/'helper').symlink_to(root/'codex')
        req['args']['command'] = './helper'
    elif kind == 'env':
        req['args'].update(command='$RUNNER exec task', env={'RUNNER':'codex', 'MCP_BLOCK_LOCAL_CODEX':'0'})
    elif kind == 'legacy':
        req.pop('execution_policy')
    elif kind == 'spoofed_project':
        req['project']['_execution_policy'] = policy('panel', False)
    spawn = AsyncMock(side_effect=AssertionError('No process may start in this test'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    await agent.handle(req)
    result = agent.journal.status(req['id'])['result']
    assert result['error']['code'] == 'CODEX_REMOTE_DISABLED', result
    spawn.assert_not_called()
    assert not (root/'never-created').exists()


@pytest.mark.asyncio
async def test_actual_benign_process_runs_and_receipt_replay_survives_policy_change(shell_agent):
    agent, root = shell_agent
    agent.config['mcp_policy'] = {'block_local_codex': True}
    req = shell_request(root, "printf '%s' 'codex'; printf once >> count")
    req['execution_policy'] = policy()
    await agent.handle(req)
    result = agent.journal.status(req['id'])['result']
    assert result['ok'] and result['data']['exit_code'] == 0 and result['data']['output'] == 'codex'
    req['execution_policy'] = policy(blocked=False)
    await agent.handle(req)
    assert (root/'count').read_text() == 'once'
    assert agent.journal.status(req['id'])['result'] == result


@pytest.mark.asyncio
async def test_task_and_file_tools_with_mentions_stay_available(shell_agent):
    agent, root = shell_agent
    agent.config['mcp_policy'] = {'block_local_codex': True}
    agent.config['tasks']['normal'] = {'projects':['fixture'], 'command':[sys.executable, '-c', 'print("codex")']}
    req = shell_request(root)
    req.update(tool='tasks_run', execution_policy=policy())
    req['args'] = {'project':'fixture', 'task':'normal', 'idempotency_key':uuid.uuid4().hex}
    await agent.handle(req)
    result = agent.journal.status(req['id'])['result']
    assert result['ok'] and result['data']['output'] == 'codex\n'
    (root/'note.txt').write_text('codex is text, not a process')
    req = shell_request(root)
    req.update(tool='fs_read', execution_policy=policy())
    req['args'] = {'project':'fixture', 'path':'note.txt'}
    await agent.handle(req)
    assert agent.journal.status(req['id'])['result']['ok']


@pytest.mark.asyncio
async def test_computer_probe_blocked_before_provider(shell_agent, monkeypatch):
    agent, root = shell_agent
    agent.config['mcp_policy'] = {'block_local_codex': True}
    req = shell_request(root)
    req.update(tool='computer_status', execution_policy=policy())
    req['args'] = {'project':'fixture', 'probe':True}
    provider = AsyncMock()
    monkeypatch.setattr(agent.computer, 'execute', provider)
    await agent.handle(req)
    assert agent.journal.status(req['id'])['result']['error']['code'] == 'CODEX_REMOTE_DISABLED'
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_queue_is_rechecked_after_policy_tightens(runtime, monkeypatch):
    r, _, _ = runtime
    p = Principal('mcp:test:ChatGPT', 'owner', {'read','write','execute'}, ['*'])
    monkeypatch.delenv('MCP_BLOCK_LOCAL_CODEX', raising=False)
    receipt = await r.invoke('shell_exec', {'project':'Project', 'command':'codex exec never-run', 'idempotency_key':'queued-policy-test'}, p)
    monkeypatch.setenv('MCP_BLOCK_LOCAL_CODEX', '1')
    await r.deliver(receipt['operation_id'])
    op = r.operation(receipt['operation_id'], p)
    assert op['state'] == 'failed' and op['result']['error']['code'] == 'CODEX_REMOTE_DISABLED'
    assert op['attempts'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('panel', [True, False])
async def test_trusted_metadata_injected_outside_idempotency_payload(runtime, monkeypatch, panel):
    r, admin, secret = runtime
    p = admin if panel else Principal('mcp:test:ChatGPT', 'owner', {'read','write','execute'}, ['*'])
    monkeypatch.setenv('MCP_BLOCK_LOCAL_CODEX', '1')
    args = {'project':'Project', 'command':'echo normal', 'idempotency_key':'metadata-receipt-test'}
    receipt = await r.invoke('shell_exec', args, p)
    original = r.store.one('SELECT fingerprint,payload FROM operations WHERE id=?', (receipt['operation_id'],))
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, 'dev'))
    try:
        await until(lambda: r.online('dev'))
        await r.deliver(receipt['operation_id'])
        packet = next(x for x in socket.packets if x['type'] == 'call')
        assert packet['execution_policy'] == policy('panel' if panel else 'mcp', not panel)
        assert 'execution_policy' not in json.loads(r.store.decrypt(original['payload']))
        assert (await r.invoke('shell_exec', args, p))['operation_id'] == receipt['operation_id']
        assert r.store.one('SELECT fingerprint FROM operations WHERE id=?', (receipt['operation_id'],))['fingerprint'] == original['fingerprint']
    finally:
        await socket.close()
        await reader
