"""Bounded SSH retries and separate transport evidence; no production hosts."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest

from agent.ssh import SSHDiagnostics, prepare_ssh, run_ssh_attempts
from shared.contracts import SSHExec
from tests.test_shell import shell_agent  # noqa: F401
from tests.test_ssh import PASSWORD, request


def transport(directory):
    directory.mkdir()
    (directory / 'sshpass').write_text(f'#!{sys.executable}\n'
        'import os,sys\nassert sys.argv[1] == "-e"\nos.execv(sys.argv[2],sys.argv[2:])\n')
    (directory / 'ssh').write_text(f'#!{sys.executable}\n' + '''
import os,subprocess,sys
from pathlib import Path
a=sys.argv[1:]
assert os.environ['SSHPASS']
assert os.environ['SSH_ASKPASS_REQUIRE'] == 'never'
counter=Path('connection-count')
n=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(n))
log=Path(a[a.index('-E')+1])
assert log.stat().st_mode & 0o777 == 0o600
assert log.parent.stat().st_mode & 0o777 == 0o700
mode=os.environ['FIXTURE_SSH_MODE']
if mode == 'auth' or mode == 'auth_then_success' and n < 3:
    log.write_text('root@fixture.invalid: Permission denied (publickey,password).\\n')
    sys.exit(255)
if mode == 'connection_then_success' and n < 3:
    log.write_text('ssh: connect to host fixture.invalid port 22: Connection refused\\n')
    sys.exit(255)
if mode == 'host_key':
    log.write_text('REMOTE HOST IDENTIFICATION HAS CHANGED!\\nHost key verification failed.\\n')
    sys.exit(255)
if mode == 'unknown':
    log.write_text('Connection closed by 192.0.2.1 port 22\\n')
    sys.exit(255)
if mode == 'missing_log':
    log.unlink()
    print('root@fixture.invalid: Permission denied (publickey,password).')
    sys.exit(255)
if mode == 'truncated':
    log.write_text('x'*100000+'\\nroot@fixture.invalid: Permission denied (password).\\n')
    sys.exit(255)
if mode == 'secret':
    log.write_text(os.environ['SSHPASS']+'\\n'+os.environ['GH_TOKEN']+'\\nConnection closed\\n')
    sys.exit(255)
log.write_text('Authenticated to fixture.invalid using "password".\\n')
if mode == 'after_auth_disconnect':
    log.write_text(log.read_text()+'Connection reset by peer\\n')
    sys.exit(255)
sys.exit(subprocess.call(['/bin/sh','-c',a[-1]]))
''')
    for path in directory.iterdir():
        path.chmod(0o700)
    return str(directory) + os.pathsep + os.defpath


def setup_transport(agent, tmp_path, mode, monkeypatch):
    agent.config['shell']['env'] = {'PATH': transport(tmp_path / 'bin'), 'FIXTURE_SSH_MODE': mode}
    monkeypatch.setattr('agent.ssh.SSH_RETRY_DELAYS', (.01, .02))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['auth_then_success', 'connection_then_success'])
async def test_transient_failure_retries_inside_same_operation_and_executes_once(shell_agent, tmp_path, monkeypatch, mode):
    agent, root = shell_agent
    setup_transport(agent, tmp_path, mode, monkeypatch)
    req = request(root, command="printf once >> remote-count; printf '{\"ok\":true}'")
    await agent.handle(req)
    await agent.handle(req)
    data = agent.journal.status(req['id'])['result']['data']
    assert data['exit_code'] == 0 and json.loads(data['output']) == {'ok': True}
    assert (root / 'remote-count').read_text() == 'once'
    assert (root / 'connection-count').read_text() == '3'
    diagnostic = data['ssh_diagnostics']
    assert diagnostic['attempt_count'] == 3 and diagnostic['attempt_limit'] == 3
    assert [row['exit_code'] for row in diagnostic['attempts']] == [255, 255, 0]
    assert diagnostic['retry_stop_reason'] == 'succeeded' and not diagnostic['retry_exhausted']
    events = agent.telemetry.snapshot(req['id'])
    assert len([row for row in events if row['stage'] == 'ssh_attempt_finished']) == 3
    assert not list(agent.state_dir.glob('ssh-diagnostic-*'))


@pytest.mark.asyncio
async def test_persistent_auth_failure_is_bounded_and_receipt_replay_does_not_reconnect(shell_agent, tmp_path, monkeypatch):
    agent, root = shell_agent
    setup_transport(agent, tmp_path, 'auth', monkeypatch)
    req = request(root, command='touch must-not-exist')
    await agent.handle(req)
    await agent.handle(req)
    data = agent.journal.status(req['id'])['result']['data']
    assert data['ssh_error'] == 'SSH_AUTH_FAILED' and data['exit_code'] == 255
    assert (root / 'connection-count').read_text() == '3'
    assert not (root / 'must-not-exist').exists()
    assert 'Permission denied' in data['output']
    assert data['ssh_diagnostics']['retry_exhausted']
    assert data['ssh_diagnostics']['retry_stop_reason'] == 'attempt_limit'
    assert not list(agent.state_dir.glob('ssh-diagnostic-*'))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['host_key', 'unknown', 'missing_log', 'truncated', 'after_auth_disconnect'])
async def test_host_identity_and_uncertain_outcomes_never_retry(shell_agent, tmp_path, monkeypatch, mode):
    agent, root = shell_agent
    setup_transport(agent, tmp_path, mode, monkeypatch)
    req = request(root, command='touch must-not-exist')
    await agent.handle(req)
    data = agent.journal.status(req['id'])['result']['data']
    assert data['exit_code'] == 255
    assert data['ssh_diagnostics']['attempt_count'] == 1
    assert (root / 'connection-count').read_text() == '1'
    assert not (root / 'must-not-exist').exists()
    assert not list(agent.state_dir.glob('ssh-diagnostic-*'))
    if mode == 'host_key':
        assert data['ssh_error'] == 'SSH_HOST_KEY_CHANGED'
    if mode == 'missing_log':
        assert data['ssh_error'] == 'SSH_COMMAND_OR_CONNECTION_FAILED'
        assert not data['ssh_diagnostics']['log_available']


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code', [1, 5, 255])
async def test_remote_program_auth_error_text_is_not_transport_failure(shell_agent, tmp_path, monkeypatch, exit_code):
    agent, root = shell_agent
    setup_transport(agent, tmp_path, 'success', monkeypatch)
    req = request(root, command=f"printf once >> remote-count; printf 'Permission denied (publickey,password).\\n' >&2; exit {exit_code}")
    await agent.handle(req)
    data = agent.journal.status(req['id'])['result']['data']
    assert data['exit_code'] == exit_code and data['ssh_error'] == 'SSH_COMMAND_OR_CONNECTION_FAILED'
    assert data['ssh_diagnostics']['authentication'] == 'succeeded'
    assert data['ssh_diagnostics']['attempt_count'] == 1
    assert (root / 'remote-count').read_text() == 'once'


@pytest.mark.asyncio
async def test_cancel_during_backoff_does_not_start_next_attempt(shell_agent, tmp_path, monkeypatch):
    agent, root = shell_agent
    setup_transport(agent, tmp_path, 'auth', monkeypatch)
    monkeypatch.setattr('agent.ssh.SSH_RETRY_DELAYS', (5.0, 5.0))
    req = request(root)
    task = asyncio.create_task(agent.handle(req))
    try:
        async with asyncio.timeout(5):
            while not any(row['stage'] == 'ssh_retry_wait' for row in agent.telemetry.snapshot(req['id'])):
                await asyncio.sleep(.01)
            agent.journal.cancel(req['id'])
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    data = agent.journal.status(req['id'])['result']['data']
    assert data['cancelled'] and data['ssh_error'] == 'SSH_CANCELLED'
    assert data['ssh_diagnostics']['attempt_count'] == 1
    assert (root / 'connection-count').read_text() == '1'
    assert not agent.processes and not list(agent.state_dir.glob('ssh-diagnostic-*'))


@pytest.mark.asyncio
async def test_deadline_includes_retry_backoff(shell_agent, tmp_path, monkeypatch):
    agent, root = shell_agent
    setup_transport(agent, tmp_path, 'auth', monkeypatch)
    monkeypatch.setattr('agent.ssh.SSH_RETRY_DELAYS', (5.0, 5.0))
    req = request(root, timeout_seconds=1)
    started = time.monotonic()
    await asyncio.wait_for(agent.handle(req), 5)
    data = agent.journal.status(req['id'])['result']['data']
    assert time.monotonic() - started < 4
    assert data['timed_out'] and data['ssh_error'] == 'SSH_TIMEOUT'
    assert data['ssh_diagnostics']['attempt_count'] == 1
    # Under high runner load the one-second deadline may expire before the
    # nested SSH fixture interpreter starts. Either way, it cannot reconnect.
    counter = root / 'connection-count'
    assert not counter.exists() or counter.read_text() == '1'


@pytest.mark.asyncio
async def test_retry_backoff_deadline_after_a_proven_auth_failure(monkeypatch):
    # Test the actual backoff path without relying on OS subprocess startup
    # to fit inside a tight wall-clock deadline on a saturated CI runner.
    monkeypatch.setattr('agent.ssh.SSH_RETRY_DELAYS', (5.0, 5.0))
    calls = []
    events = []

    async def attempt(remaining):
        calls.append(remaining)
        return {'exit_code': 255, 'output': '', 'ssh_error': 'SSH_AUTH_FAILED',
                'ssh_diagnostics': {'retryable': True, 'authentication': 'failed',
                                    'failure_stage': 'authentication'}}

    def record(stage, **detail):
        events.append((stage, detail))

    started = time.monotonic()
    data = await run_ssh_attempts(attempt, timeout=1, cancelled=lambda: False,
                                  stopping=lambda: False, record=record)
    assert len(calls) == 1
    assert data['timed_out'] and data['ssh_error'] == 'SSH_TIMEOUT'
    assert data['ssh_diagnostics']['attempt_count'] == 1
    assert data['ssh_diagnostics']['retry_stop_reason'] == 'timeout'
    assert [stage for stage, _ in events].count('ssh_retry_wait') == 1
    assert [stage for stage, _ in events].count('ssh_attempt') == 1
    assert time.monotonic() - started < 4


@pytest.mark.asyncio
async def test_client_log_credentials_are_redacted_before_persistence(shell_agent, tmp_path, monkeypatch):
    agent, root = shell_agent
    setup_transport(agent, tmp_path, 'secret', monkeypatch)
    agent.config['shell']['env'].update(GH_TOKEN='fixture-github-secret', SSH_ASKPASS_REQUIRE='force')
    req = request(root)
    await agent.handle(req)
    state = agent.journal.status(req['id'])
    assert '<redacted>' in state['result']['data']['output']
    persisted = '\n'.join(agent.journal.db.iterdump())
    assert PASSWORD not in persisted and 'fixture-github-secret' not in persisted
    assert not list(agent.state_dir.glob('ssh-diagnostic-*'))


def test_real_openssh_effective_options_ignore_conflicting_auth_and_session_settings(shell_agent, tmp_path):
    ssh = shutil.which('ssh')
    if not ssh:
        pytest.skip('OpenSSH is required for offline configuration validation')
    agent, root = shell_agent
    # Only OpenSSH -G is executed here; sshpass is not. Supply a non-executed
    # resolver fixture so macOS runners without sshpass still verify real
    # OpenSSH's effective options instead of skipping a release-gate test.
    fixture_bin = tmp_path / 'bin'
    fixture_bin.mkdir()
    sshpass = fixture_bin / 'sshpass'
    sshpass.write_text('#!/bin/sh\nexit 99\n')
    sshpass.chmod(0o700)
    agent.config['shell']['env'] = {
        'PATH': str(fixture_bin) + os.pathsep + str(Path(ssh).parent) + os.pathsep + os.defpath,
    }
    config = tmp_path / 'ssh_config'
    config.write_text('Host *\n PasswordAuthentication no\n KbdInteractiveAuthentication no\n'
        ' ControlMaster auto\n ControlPath /tmp/unrelated-fixture-socket\n ControlPersist yes\n'
        ' ConnectionAttempts 9\n ForkAfterAuthentication yes\n SessionType none\n RemoteCommand echo unexpected\n')
    req = request(root)
    argv, environment = prepare_ssh(agent.config, req['project'], agent.config['allowed_roots'][0], SSHExec(**req['args']).model_dump())
    result = subprocess.run([ssh, '-G', '-F', str(config), *argv[3:-1]],
        capture_output=True, text=True, timeout=5, check=True)
    effective = dict(line.split(' ', 1) for line in result.stdout.splitlines())
    for key, expected in {'passwordauthentication': 'yes', 'kbdinteractiveauthentication': 'yes',
        'controlmaster': 'false', 'controlpersist': 'no', 'connectionattempts': '1',
        'forkafterauthentication': 'no', 'sessiontype': 'default', 'numberofpasswordprompts': '1',
        'stricthostkeychecking': 'true'}.items():
        assert effective[key] == expected
    assert effective.get('controlpath', 'none') == 'none'
    assert effective.get('remotecommand', 'none') == 'none'
    assert environment['SSH_ASKPASS_REQUIRE'] == 'never'


def test_real_openssh_connection_failure_provides_separate_retry_evidence(tmp_path):
    ssh = shutil.which('ssh')
    if not ssh:
        pytest.skip('OpenSSH is required for the loopback transport diagnostic')
    # Reserve a loopback port without listening; no user or server credentials.
    # Depending on the host firewall, OpenSSH observes refusal or timeout.
    with socket.socket() as reserved, SSHDiagnostics(tmp_path) as diagnostic:
        reserved.bind(('127.0.0.1', 0))
        result = subprocess.run([ssh, '-F', 'none', '-E', str(diagnostic.path), '-o', 'LogLevel=VERBOSE',
            '-o', 'ConnectTimeout=1', '-p', str(reserved.getsockname()[1]), '127.0.0.1', 'true'],
            capture_output=True, text=True, timeout=5)
        data = diagnostic.complete({'exit_code': result.returncode, 'output': result.stdout + result.stderr},
            environment={}, credential_source='explicit_password')
    assert data['ssh_diagnostics']['retryable'] and data['ssh_diagnostics']['failure_stage'] == 'connection'
    assert any(message in data['output'] for message in ('Connection refused', 'Operation timed out', 'Connection timed out'))
    assert not list(tmp_path.glob('ssh-diagnostic-*'))


def test_vps_response_wait_does_not_change_normalized_execution_arguments():
    from shared.core_contracts import Exec
    base = {'project': 'fixture', 'command': 'true', 'idempotency_key': 'wait-contract'}
    assert Exec(**base).yield_seconds == 1
    # The Hub's response wait is separate from the persisted request fingerprint.
    assert Exec(**base, target='vps:' + 'a' * 32).yield_seconds == 1
    for seconds in (0, 1, 10):
        assert Exec(**base, target='vps:' + 'a' * 32, yield_seconds=seconds).yield_seconds == seconds


def test_attempt_metadata_survives_mcp_and_durable_trace_without_private_fields(shell_agent):
    from hub.diagnostics import event_detail
    from shared.mcp_presentation import present
    agent, _ = shell_agent
    attempt = {'attempt': 1, 'exit_code': 255, 'ssh_error': 'SSH_AUTH_FAILED',
        'authentication': 'failed', 'failure_stage': 'authentication', 'retryable': True,
        'password': 'do-not-export', 'host': 'private-host'}
    raw = {'exit_code': 255, 'output': '', 'ssh_diagnostics': {
        'attempt_count': 3, 'attempt_limit': 3, 'attempts': [attempt] * 4,
        'password': 'do-not-export', 'authentication': 'failed', 'retry_exhausted': True},
        'transport_runtime': {'mode': 'native_ssh_transport', 'target': 'vps:' + 'a' * 32,
                              'isolation': 'none', 'grants_authority': False, 'home': '/private'}}
    direct = present('exec', {}, raw)
    polled = present('task_query', {'operation': 'get'}, {'operations': [
        {'id': 'operation', 'tool': 'exec', 'state': 'failed', 'result': {'ok': True, 'data': raw}}]})
    assert polled['operations'][0]['result']['data'] == direct
    assert len(direct['ssh_diagnostics']['attempts']) == 3
    assert direct['transport_runtime']['mode'] == 'native_ssh_transport'
    assert 'private' not in json.dumps(direct) and 'do-not-export' not in json.dumps(direct)
    agent.telemetry.record('operation', 'ssh_attempt_finished', **attempt)
    recorded = agent.telemetry.snapshot('operation')[0]['detail']
    assert event_detail('ssh_attempt_finished', recorded) == direct['ssh_diagnostics']['attempts'][0]
    assert 'do-not-export' not in '\n'.join(agent.journal.db.iterdump())


def test_cleanup_failure_preserves_original_result_and_stops_retry(tmp_path, monkeypatch):
    capture = SSHDiagnostics(tmp_path)
    with capture:
        capture.path.write_text('root@fixture.invalid: Permission denied (password).\n')
        data = capture.complete({'exit_code': 255, 'output': ''}, environment={}, credential_source='saved_vps')
        original_cleanup = capture.temporary.cleanup
        def fail_cleanup():
            raise OSError('fixture cleanup unavailable')
        monkeypatch.setattr(capture.temporary, 'cleanup', fail_cleanup)
    try:
        assert data['exit_code'] == 255 and data['ssh_error'] == 'SSH_AUTH_FAILED'
        assert data['ssh_diagnostics']['log_cleanup'] == 'pending'
        assert not data['ssh_diagnostics']['retryable']
    finally:
        original_cleanup()


@pytest.mark.asyncio
async def test_client_log_growth_is_bounded_and_cannot_enable_retries(tmp_path):
    async with SSHDiagnostics(tmp_path) as capture:
        capture.path.write_text('Authenticated to fixture.invalid using "password".\n' + 'x' * (5 * capture.limit))
        async with asyncio.timeout(3):
            while not capture.overflowed:
                await asyncio.sleep(.01)
        assert capture.path.stat().st_size <= capture.limit
        with capture.path.open('a') as log:
            log.write('root@fixture.invalid: Permission denied (password).\n')
        data = capture.complete({'exit_code': 255, 'output': ''}, environment={}, credential_source='saved_vps')
    assert data['ssh_error'] == 'SSH_COMMAND_OR_CONNECTION_FAILED'
    assert data['ssh_diagnostics']['authentication'] == 'succeeded'
    assert data['ssh_diagnostics']['log_truncated'] and not data['ssh_diagnostics']['retryable']
    assert data['ssh_diagnostics']['log_cleanup'] == 'removed'


@pytest.mark.asyncio
async def test_shutdown_during_backoff_retains_attempt_evidence_without_replay(shell_agent, tmp_path, monkeypatch):
    agent, root = shell_agent
    setup_transport(agent, tmp_path, 'auth', monkeypatch)
    monkeypatch.setattr('agent.ssh.SSH_RETRY_DELAYS', (5.0, 5.0))
    req = request(root)
    task = asyncio.create_task(agent.handle(req))
    try:
        async with asyncio.timeout(5):
            while not any(row['stage'] == 'ssh_retry_wait' for row in agent.telemetry.snapshot(req['id'])):
                await asyncio.sleep(.01)
            agent.stop_event.set()
            await task
        result = agent.journal.status(req['id'])['result']
        assert result['error']['code'] == 'INTERRUPTED'
        assert any(row['stage'] == 'ssh_attempt_finished' for row in result['trace'])
        assert (root / 'connection-count').read_text() == '1'
        assert not list(agent.state_dir.glob('ssh-diagnostic-*'))
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
