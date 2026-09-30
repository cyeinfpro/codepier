"""Password SSH uses real child processes and an isolated MCP stack, no live VPS."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import uuid

import pytest
from pydantic import ValidationError

from agent.ssh import prepare_ssh, ssh_error
from shared.contracts import SSHExec, tool_definitions
from shared.contracts import ShellExec, _compact_input_schema
from shared.secret_output import SecretOutput
from shared.util import DevError, atomic_json, safe_summary
from tests.test_shell import shell_agent  # noqa: F401
from tests.support import wait_for


PASSWORD = "fixture-only-'$; with spaces"


def args(**overrides):
    return {'project': 'fixture', 'host': 'vps.example.invalid', 'username': 'root',
            'password': PASSWORD, 'command': 'printf remote-ok',
            'idempotency_key': uuid.uuid4().hex, **overrides}


def fake_transport(directory):
    directory.mkdir()
    # Password is supplied ONLY via env; the transport checks argv and executes
    # a local fixture command to exercise the process lifecycle deterministically.
    (directory / 'sshpass').write_text(f'#!{sys.executable}\n'
        'import os,sys\nassert sys.argv[1] == "-e"\nos.execv(sys.argv[2],sys.argv[2:])\n')
    (directory / 'ssh').write_text(f'#!{sys.executable}\n'
        'import os,sys,subprocess,time\n'
        'a=sys.argv[1:]; p=os.environ["SSHPASS"]\n'
        'assert p not in " ".join(a)\n'
        'assert "BatchMode=no" in a and "PubkeyAuthentication=no" in a\n'
        'assert "StrictHostKeyChecking=yes" in a or "StrictHostKeyChecking=accept-new" in a\n'
        'if p == "wrong":\n'
        ' print("Permission denied, please try again.",flush=True);sys.exit(5)\n'
        'assert a[a.index("--")+1] == "vps.example.invalid"\n'
        'print("transport-ready",flush=True)\n'
        'sys.stdout.write(p[:8]);sys.stdout.flush();time.sleep(.3)\n'
        'sys.stdout.write(p[8:]+"\\n");sys.stdout.flush()\n'
        'sys.exit(subprocess.call(["/bin/sh","-c",a[-1]]))\n')
    for file in directory.iterdir():
        file.chmod(0o700)
    return str(directory) + os.pathsep + os.defpath


@pytest.mark.parametrize('overrides', [
    {'host': '-oProxyCommand=evil'}, {'host': 'a;id'}, {'host': 'user@host'},
    {'host': 'https://host'}, {'host': 'host:22'}, {'host': 'a b'},
    {'username': '-root'}, {'username': 'root@host'}, {'port': 0}, {'port': True},
    {'password': ''}, {'password': 'p\nnext'}, {'password': 'p\x00'},
    {'command': '\x00'}, {'command': ' '}, {'host_key_policy': 'no'},
])
def test_bad_ssh_arguments(overrides):
    with pytest.raises(ValidationError):
        SSHExec(**args(**overrides))


@pytest.mark.parametrize('host', ['127.0.0.1', '::1', '2001:db8::1', 'vps.example.invalid', 'host-name'])
def test_ssh_hosts_and_credential_repr(host):
    value = SSHExec(**args(host=host))
    assert PASSWORD not in repr(value)
    assert PASSWORD not in json.dumps(safe_summary(value.model_dump()))


@pytest.mark.parametrize('secret', ['abcabc', 'x', PASSWORD, '中文🔑密码'])
def test_stream_redaction_all_split_positions(secret):
    value = 'before:' + secret + ':between:' + secret + ':after'
    for split in range(len(value) + 1):
        redactor = SecretOutput(secret)
        first = redactor.feed(value[:split])
        second = redactor.feed(value[split:])
        output = first + second + redactor.feed('', final=True)
        assert output == value.replace(secret, '<redacted>')


def request(root, **overrides):
    return {'id': uuid.uuid4().hex, 'tool': 'ssh_exec',
            'project': {'root': str(root), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True},
            'args': args(**overrides)}


@pytest.mark.asyncio
@pytest.mark.parametrize('denial', ['disabled', 'unlisted', 'local_tasks', 'project_tasks', 'local_readonly', 'project_readonly'])
async def test_ssh_permission_denial_before_spawn(shell_agent, monkeypatch, denial):
    agent, root = shell_agent
    req = request(root)
    if denial == 'disabled': agent.config['shell']['enabled'] = False
    elif denial == 'unlisted': agent.config['shell']['projects'] = []
    elif denial == 'local_tasks': agent.config['allowed_roots'][0]['allow_tasks'] = False
    elif denial == 'project_tasks': req['project']['allow_tasks'] = False
    elif denial == 'local_readonly': agent.config['allowed_roots'][0]['writable'] = False
    else: req['project']['mode'] = 'read'
    async def trap(*a, **kw):
        pytest.fail('Unauthorized SSH reached process creation')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', trap)
    await agent.handle(req)
    result = agent.journal.status(req['id'])['result']
    assert not result['ok']
    assert result['error']['code'] in {'SHELL_DISABLED', 'SHELL_NOT_ALLOWED', 'TASKS_DISABLED', 'READ_ONLY'}


def test_ssh_dependencies_and_timeout(shell_agent, monkeypatch):
    agent, root = shell_agent
    req = request(root)
    value = SSHExec(**req['args']).model_dump()
    monkeypatch.setattr('agent.ssh.shutil.which', lambda *a, **k: None)
    with pytest.raises(DevError, match='sshpass') as caught:
        prepare_ssh(agent.config, req['project'], agent.config['allowed_roots'][0], value)
    assert caught.value.code == 'SSH_DEPENDENCY_MISSING'
    agent.config['shell']['max_timeout_seconds'] = 1
    with pytest.raises(DevError) as caught:
        prepare_ssh(agent.config, req['project'], agent.config['allowed_roots'][0], value)
    assert caught.value.code == 'SHELL_TIMEOUT_LIMIT'


@pytest.mark.asyncio
async def test_ssh_process_credentials_redaction_and_idempotency(shell_agent, tmp_path):
    agent, root = shell_agent
    agent.config['shell']['env'] = {'PATH': fake_transport(tmp_path / 'bin')}
    # The local Codex guard remains enabled and does not block ordinary SSH.
    agent.config['mcp_policy']['block_local_codex'] = True
    req = request(root, command='printf once >> count; printf remote-ok; exit 7')
    await agent.handle(req)
    await agent.handle(req)
    state = agent.journal.status(req['id'])
    data = state['result']['data']
    assert data['exit_code'] == 7 and data['ssh_error'] == 'SSH_COMMAND_OR_CONNECTION_FAILED'
    assert 'remote-ok' in data['output'] and '<redacted>' in data['output']
    assert (root / 'count').read_text() == 'once'
    assert PASSWORD not in json.dumps(state)
    assert PASSWORD not in json.dumps(agent.pending_output)
    assert PASSWORD not in str(agent.journal.db.execute('SELECT * FROM calls').fetchall())


@pytest.mark.asyncio
async def test_invalid_password_never_leaks_validation_input(shell_agent):
    agent, root = shell_agent
    req = request(root, password=PASSWORD + '\n')
    await agent.handle(req)
    result = agent.journal.status(req['id'])['result']
    assert result['error']['code'] == 'INVALID_ARGUMENTS'
    assert PASSWORD not in json.dumps(result)


def test_ssh_catalog_and_error_classification():
    for profile in ('full', 'coding'):
        tool = next(t for t in tool_definitions(profile) if t['name'] == 'exec')
        assert not tool['annotations']['readOnlyHint']
        assert tool['annotations']['destructiveHint'] and tool['annotations']['openWorldHint']
        assert set(tool['securitySchemes'][0]['scopes']) == {'read', 'execute'}
    for output, code in [('Permission denied (password).', 'SSH_AUTH_FAILED'),
                         ('Host key verification failed.', 'SSH_HOST_KEY_UNTRUSTED'),
                         ('REMOTE HOST IDENTIFICATION HAS CHANGED!', 'SSH_HOST_KEY_CHANGED'),
                         ('remote exit 5', 'SSH_COMMAND_OR_CONNECTION_FAILED')]:
        assert ssh_error({'exit_code': 5, 'output': output}) == code


def test_compact_schema_preserves_named_title_fields_and_validation():
    schema = {'title': 'Display', 'type': 'object', 'properties': {
        'title': {'title': 'User title', 'type': 'string', 'minLength': 1}}, 'required': ['title']}
    compact = _compact_input_schema(schema)
    assert compact['properties']['title'] == {'type': 'string', 'minLength': 1}
    assert compact['required'] == ['title'] and schema['title'] == 'Display'


@pytest.mark.asyncio
async def test_existing_local_script_password_env_is_redacted(shell_agent):
    agent, root = shell_agent
    req = request(root)
    req['tool'] = 'shell_exec'
    req['args'] = ShellExec(project='fixture', command='printf "%s" "$SSHPASS"',
                            env={'SSHPASS': PASSWORD}, idempotency_key=uuid.uuid4().hex).model_dump()
    await agent.handle(req)
    state = agent.journal.status(req['id'])
    assert state['result']['data']['output'] == '<redacted>'
    assert PASSWORD not in json.dumps(state)


def test_mcp_ssh_recovery_cancel_scope_and_secret_storage(stack):
    stack.stop_agent()
    path = fake_transport(stack.directory / 'ssh-bin')
    stack.config['shell'] = {'enabled': True, 'projects': ['Imago'], 'command': ['/bin/sh', '-c'], 'env': {'PATH': path}}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    from tests.test_vps import create_remote
    remote = create_remote(stack)
    invalid_remote = create_remote(stack, password='wrong')
    def execution(command='printf remote-ok', wrong=False):
        return {'project':'Imago','target':'vps:'+ (invalid_remote if wrong else remote)['id'],
                'command':command,'yield_seconds':0,'idempotency_key':uuid.uuid4().hex}
    call = execution(command='printf once >> ssh-count; sleep 2; printf remote-ok')
    receipt = stack.mcp('exec', call)['structuredContent']
    opid = receipt['operation_id']
    assert receipt['pending']
    wait_for(lambda: 'transport-ready' in stack.client.get('/api/operations/' + opid).json()['output'])
    assert stack.mcp('exec', call)['structuredContent']['operation_id'] == opid
    stack.hub.terminate(); stack.hub.wait(timeout=12); stack.start_hub(); stack.login()
    completed = stack.poll(opid, timeout=15)
    assert completed['state'] == 'succeeded'
    assert completed['result']['data']['command_ok']
    assert PASSWORD not in json.dumps(completed)
    assert (stack.imago / 'ssh-count').read_text() == 'once'
    failed = stack.mcp('exec', execution(wrong=True))['structuredContent']['operation_id']
    result = stack.poll(failed)
    assert result['state'] == 'failed' and result['result']['data']['ssh_error'] == 'SSH_AUTH_FAILED'
    cancelled = stack.mcp('exec', execution(command='sleep 20; touch must-not-exist'))['structuredContent']['operation_id']
    wait_for(lambda: 'transport-ready' in stack.client.get('/api/operations/' + cancelled).json()['output'])
    stack.mcp('process', {'operation': 'cancel', 'operation_ids': [cancelled]})
    assert stack.poll(cancelled)['state'] == 'cancelled'
    assert not (stack.imago / 'must-not-exist').exists()
    grant = stack.must(stack.client.post('/api/grants', json={'label': 'ssh-readonly', 'scopes': ['read'], 'projects': [stack.project['id']], 'days': 1}))
    denied = stack.mcp('exec', execution(), token_value=grant['token'])
    assert denied['isError'] and 'execute' in denied['content'][0]['text']
    stack.stop_agent()
    grant = stack.must(stack.client.post('/api/grants', json={'label': 'ssh-revoke', 'scopes': ['read', 'execute'], 'projects': [stack.project['id']], 'days': 1}))
    queued = stack.mcp('exec', execution(command='touch revoked-ssh'), token_value=grant['token'])['structuredContent']['operation_id']
    stack.must(stack.client.delete('/api/grants/' + grant['grant_id']))
    stack.start_agent()
    assert stack.poll(queued)['state'] == 'failed'
    assert not (stack.imago / 'revoked-ssh').exists()
    # Inspect actual persisted rows, not only the API's filtered projection.
    for dbpath in (stack.directory / 'agent-state' / 'agent.sqlite3', stack.hubdir / 'hub.sqlite3'):
        assert dbpath.exists()
        with sqlite3.connect(dbpath) as db:
            assert PASSWORD not in '\n'.join(db.iterdump())
