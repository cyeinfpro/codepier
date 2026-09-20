"""Password SSH over the existing full-access, durable process execution path."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from agent.shell import shell_denial
from shared.util import DevError


def prepare_ssh(config, project, spec, args):
    denial = shell_denial(config, project, spec)
    if denial:
        raise DevError(*denial, 403)
    shell = config['shell']
    if args['timeout_seconds'] > shell['max_timeout_seconds']:
        raise DevError('SHELL_TIMEOUT_LIMIT', '请求超时超过本机 Shell 执行上限')
    path = shell['env'].get('PATH', os.environ.get('PATH', os.defpath))
    ssh = shutil.which('ssh', path=path)
    sshpass = shutil.which('sshpass', path=path)
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
               '-o', 'PreferredAuthentications=password,keyboard-interactive',
               '-o', 'NumberOfPasswordPrompts=1',
               '-o', 'StrictHostKeyChecking=' + ('yes' if args['host_key_policy'] == 'strict' else 'accept-new'),
               '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
               *known_hosts, '-p', str(args['port']), '-l', args['username'], '--', args['host'], args['command']]
    return command, {**shell['env'], 'SSHPASS': args['password'], 'LC_ALL': 'C'}


def ssh_error(result):
    if result.get('cancelled'):
        return 'SSH_CANCELLED'
    if result.get('timed_out'):
        return 'SSH_TIMEOUT'
    if result['exit_code'] == 0:
        return None
    output = result['output'].lower()
    if 'remote host identification has changed' in output:
        return 'SSH_HOST_KEY_CHANGED'
    if 'host key verification failed' in output:
        return 'SSH_HOST_KEY_UNTRUSTED'
    if 'permission denied (' in output or 'permission denied, please try again' in output:
        return 'SSH_AUTH_FAILED'
    # A nonzero remote command may use the same exit codes as OpenSSH/sshpass.
    # Never infer wrong credentials from the exit code alone.
    return 'SSH_COMMAND_OR_CONNECTION_FAILED'
