"""Password SSH over the existing full-access, durable process execution path."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from agent.shell import shell_denial
from shared.secret_output import SecretOutputs, credential_output_values
from shared.ssh_diagnostics import SSH_ATTEMPT_LIMIT, safe_ssh_detail
from shared.util import DevError


def ssh_transport_tools(shell):
    """Resolve native SSH dependencies without borrowing the project PATH.

    Saved-VPS transport runs in the Agent account environment even when the
    local project uses a clean runtime. Keep discovery and launch identical;
    explicit shell.env.PATH, including an empty value, overrides Agent PATH.
    """
    path = shell['env'].get('PATH', os.environ.get('PATH', os.defpath))
    return {name: shutil.which(name, path=path) for name in ('ssh', 'sshpass')}


def prepare_ssh(config, project, spec, args, *, diagnostic_path=None):
    denial = shell_denial(config, project, spec)
    if denial:
        raise DevError(*denial, 403)
    shell = config['shell']
    if args['timeout_seconds'] > shell['max_timeout_seconds']:
        raise DevError('SHELL_TIMEOUT_LIMIT', '请求超时超过本机 Shell 执行上限')
    tools = ssh_transport_tools(shell)
    ssh, sshpass = tools['ssh'], tools['sshpass']
    if not ssh or not sshpass:
        raise DevError('SSH_DEPENDENCY_MISSING', '密码 SSH 需要 Agent 本机安装 ssh 和 sshpass，并在 Agent PATH 或 shell.env.PATH 中可见；尚未连接服务器')
    known_hosts = []
    if args['known_hosts_file']:
        path = Path(args['known_hosts_file']).expanduser()
        if not path.is_absolute():
            raise DevError('SSH_KNOWN_HOSTS_PATH', 'known_hosts_file 必须是 Agent 本机绝对路径')
        # OpenSSH parses whitespace within -o values; quote the single path.
        if '"' in str(path) or '\\' in str(path):
            raise DevError('SSH_KNOWN_HOSTS_PATH', 'known_hosts_file 不能包含双引号或反斜杠')
        known_hosts = ['-o', f'UserKnownHostsFile="{path}"']
    command = [sshpass, '-e', ssh, '-T',
               '-o', 'BatchMode=no', '-o', 'PubkeyAuthentication=no',
               '-o', 'PasswordAuthentication=yes', '-o', 'KbdInteractiveAuthentication=yes',
               '-o', 'PreferredAuthentications=password,keyboard-interactive',
               '-o', 'NumberOfPasswordPrompts=1',
               '-o', 'ConnectionAttempts=1',
               '-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ControlPersist=no',
               '-o', 'ForkAfterAuthentication=no', '-o', 'SessionType=default', '-o', 'RemoteCommand=none',
               '-o', 'StrictHostKeyChecking=' + ('yes' if args['host_key_policy'] == 'strict' else 'accept-new'),
               '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
               *(['-E', str(diagnostic_path), '-o', 'LogLevel=VERBOSE'] if diagnostic_path is not None else []),
               *known_hosts, '-p', str(args['port']), '-l', args['username'], '--', args['host'], args['command']]
    # sshpass owns the password prompt. A desktop/IDE askpass inherited after a
    # restart must not intercept it or prompt for a different credential.
    return command, {**shell['env'], 'SSHPASS': args['password'], 'LC_ALL': 'C', 'SSH_ASKPASS_REQUIRE': 'never'}


def ssh_error(result, *, client_output=None, authenticated=False):
    if result.get('cancelled'):
        return 'SSH_CANCELLED'
    if result.get('timed_out'):
        return 'SSH_TIMEOUT'
    if result['exit_code'] == 0:
        return None
    # Once authentication succeeded, a remote program's stderr (including a
    # nested ssh failure) cannot describe this transport's authentication.
    if authenticated:
        return 'SSH_COMMAND_OR_CONNECTION_FAILED'
    # None retains compatibility with old receipts. New executions always pass
    # the separate client log, even if it is empty or unavailable.
    output = (result.get('output', '') if client_output is None else client_output).lower()
    if 'remote host identification has changed' in output:
        return 'SSH_HOST_KEY_CHANGED'
    if 'host key verification failed' in output:
        return 'SSH_HOST_KEY_UNTRUSTED'
    if 'permission denied (' in output or 'permission denied, please try again' in output:
        return 'SSH_AUTH_FAILED'
    # A nonzero remote command may use the same exit codes as OpenSSH/sshpass.
    # Never infer wrong credentials from the exit code alone.
    return 'SSH_COMMAND_OR_CONNECTION_FAILED'


class SSHDiagnostics:
    """Private, short-lived client log, separate from untrusted remote output.

    VERBOSE records authentication without DEBUG's command/env dumps. Only a
    bounded, redacted tail is returned on failure; the temporary file is removed
    on success, failure and cancellation. Missing evidence never triggers retry.
    """

    limit = 32768
    authenticated_line = re.compile(r'^Authenticated to .+ using "(?:password|keyboard-interactive)"\.$', re.M)
    authentication_failure = re.compile(r'^(?:[^\n]+: )?Permission denied \([a-zA-Z0-9,@._-]+\)\.\s*$', re.M)
    connection_failure = re.compile(
        r'^ssh: connect to host \S+ port [0-9]+: (?:Connection refused|Connection timed out|Operation timed out|'
        r'No route to host|Network is unreachable)\s*$|'
        r'^ssh: Could not resolve hostname \S+: Temporary failure in name resolution\s*$|'
        r'^kex_exchange_identification: (?:read: )?Connection (?:reset by peer|closed by remote host)\s*$', re.M)

    def __init__(self, directory):
        self.directory = directory
        self.path = None
        self.temporary = None
        self.result = None
        self.overflowed = False
        self.monitor = None

    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ssh-diagnostic-', dir=self.directory)
        self.path = Path(self.temporary.name) / 'client.log'
        try:
            with self.path.open('xb'):
                pass
            self.path.chmod(0o600)
        except BaseException:
            self.temporary.cleanup()
            raise
        return self

    async def __aenter__(self):
        self.__enter__()
        self.monitor = asyncio.create_task(self._bound_log())
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.monitor.cancel()
        try:
            await asyncio.gather(self.monitor, return_exceptions=True)
        finally:
            self.__exit__(exc_type, exc_value, traceback)

    async def _bound_log(self):
        # OpenSSH opens -E with O_APPEND. Retain the initial complete lines
        # (including authentication evidence), bound disk growth while a command
        # runs, and mark any lost evidence ineligible for automatic retry.
        while True:
            await asyncio.sleep(.1)
            try:
                if self.path.stat().st_size <= 4 * self.limit:
                    continue
                self.overflowed = True
                with self.path.open('r+b') as stream:
                    head = stream.read(self.limit // 2)
                    stream.truncate(head.rfind(b'\n') + 1)
            except OSError:
                self.overflowed = True
                return

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.temporary.cleanup()
            cleanup = 'removed'
        except OSError:
            # A cleanup problem cannot erase a completed remote operation or
            # turn its unknown result into an invitation to run it again.
            cleanup = 'pending'
        if self.result is not None:
            self.result['ssh_diagnostics']['log_cleanup'] = cleanup
            if cleanup == 'pending':
                self.result['ssh_diagnostics']['retryable'] = False

    def complete(self, result, *, environment, credential_source):
        available, truncated = True, self.overflowed
        try:
            with self.path.open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                truncated = truncated or size > self.limit
                if size > self.limit:
                    chunk = stream.read(self.limit // 2)
                    head = chunk.rsplit(b'\n', 1)[0] if b'\n' in chunk else b''
                    stream.seek(-self.limit // 2, os.SEEK_END)
                    chunk = stream.read(self.limit // 2)
                    tail = chunk.split(b'\n', 1)[-1] if b'\n' in chunk else b''
                    raw = head + b'\n' + tail
                else:
                    raw = stream.read(self.limit)
            client_output = raw.decode('utf-8', errors='replace').replace('\r\n', '\n')
        except OSError:
            available, client_output = False, ''
        authenticated = bool(self.authenticated_line.search(client_output))
        code = ssh_error(result, client_output=client_output, authenticated=authenticated)
        preauth_connection_failure = bool(self.connection_failure.search(client_output)) and not authenticated
        retryable = bool(available and not truncated and not authenticated and
            not result.get('cancelled') and not result.get('timed_out') and
            (code == 'SSH_AUTH_FAILED' and self.authentication_failure.search(client_output) or
             code == 'SSH_COMMAND_OR_CONNECTION_FAILED' and preauth_connection_failure))
        authentication = 'succeeded' if authenticated else 'failed' if code == 'SSH_AUTH_FAILED' else 'not_observed'
        stage = ('authentication' if code == 'SSH_AUTH_FAILED' else
                 'host_verification' if code in {'SSH_HOST_KEY_CHANGED', 'SSH_HOST_KEY_UNTRUSTED'} else
                 'connection' if preauth_connection_failure else
                 'after_authentication' if authenticated else 'not_observed')
        redactor = SecretOutputs(credential_output_values(environment))
        data = {**result, 'ssh_error': code, 'ssh_diagnostics': {
            'schema_version': 1, 'source': 'openssh_client_log',
            'log_available': available, 'log_truncated': truncated,
            'authentication': authentication, 'failure_stage': stage if code else None,
            'credential_source': credential_source, 'password_prompt_limit': 1,
            'connection_attempt_limit': 1, 'automatic_replay': False,
            'retryable': retryable,
        }}
        if code and client_output:
            # Preserve actual client errors in the original operation result;
            # stream output still belongs exclusively to the running workload.
            diagnostic = redactor.redact(client_output)
            output = result.get('output', '')
            output += ('\n' if output and not output.endswith('\n') else '') + diagnostic
            data['output'] = output[-131072:]
            data['output_truncated'] = bool(result.get('output_truncated') or truncated or len(output) > 131072)
        # The temporary filename is not a durable evidence reference.
        if isinstance(data.get('command'), list):
            data['command'] = ['<ssh-client-log>' if value == str(self.path) else value for value in data['command']]
        self.result = data
        return data


SSH_RETRY_DELAYS = (1.0, 2.0)


async def run_ssh_attempts(run_attempt, *, timeout, cancelled, stopping, record):
    """Retry only proven pre-command failures within one original operation.

    The total deadline includes attempts and backoff. A successful authentication,
    host-key error, missing/truncated evidence or uncertain outcome stops retries.
    Remote stdout remains the final attempt's output so successful JSON/scripts
    are not corrupted by preceding connection errors.
    """
    started = time.monotonic()
    deadline = started + timeout
    attempts = []
    result = None
    stop_reason = 'not_retryable'
    for attempt in range(1, SSH_ATTEMPT_LIMIT + 1):
        if stopping():
            raise asyncio.CancelledError
        if cancelled() or time.monotonic() >= deadline:
            # No command has started when this branch is reached between tries.
            result = result or {'exit_code': -1, 'output': '', 'output_truncated': False, 'ssh_diagnostics': {}}
            is_cancelled = cancelled()
            result.update(cancelled=is_cancelled, timed_out=not is_cancelled,
                          ssh_error='SSH_CANCELLED' if is_cancelled else 'SSH_TIMEOUT')
            stop_reason = 'cancelled' if is_cancelled else 'timeout'
            break
        record('ssh_attempt', attempt=attempt, attempt_limit=SSH_ATTEMPT_LIMIT)
        result = await run_attempt(deadline - time.monotonic())
        diagnostic = result['ssh_diagnostics']
        summary = safe_ssh_detail({'attempt': attempt, 'exit_code': result['exit_code'],
            'duration_ms': result.get('duration_ms'), 'ssh_error': result['ssh_error'],
            'authentication': diagnostic.get('authentication'), 'failure_stage': diagnostic.get('failure_stage'),
            'retryable': diagnostic.get('retryable', False)})
        attempts.append(summary)
        record('ssh_attempt_finished', **summary)
        if not diagnostic.get('retryable'):
            stop_reason = ('cancelled' if result.get('cancelled') else 'timeout' if result.get('timed_out') else
                           'succeeded' if result['exit_code'] == 0 else 'not_retryable')
            break
        if attempt == SSH_ATTEMPT_LIMIT:
            stop_reason = 'attempt_limit'
            break
        delay = min(SSH_RETRY_DELAYS[attempt - 1], max(0, deadline - time.monotonic()))
        record('ssh_retry_wait', attempt=attempt, retry_delay_ms=round(delay * 1000))
        wait_until = time.monotonic() + delay
        while time.monotonic() < wait_until and not cancelled() and not stopping():
            await asyncio.sleep(min(.1, max(0, wait_until - time.monotonic())))
    result['duration_ms'] = round((time.monotonic() - started) * 1000)
    result['ssh_diagnostics'].update(attempt_count=len(attempts), attempt_limit=SSH_ATTEMPT_LIMIT,
        attempts=attempts, retry_policy='pre_command_failures', retry_stop_reason=stop_reason,
        retry_exhausted=stop_reason == 'attempt_limit', automatic_replay=False, retryable=False)
    return result
