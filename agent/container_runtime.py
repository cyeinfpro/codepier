"""Fail-closed Linux container adapter; no implicit image pull or native fallback.

A capability advertisement is not isolation evidence. Inspect the exact created
container before starting project code. Only the approved project is bind-mounted;
its writes remain real writes to that project. This is not a VM or an anonymity
claim. The existing Agent journal owns operation identity and replay protection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
from agent.runtime_control import run_control
import time

from shared.util import DevError, atomic_json

DRIVER = 'docker_restricted_v1'
IMAGE = re.compile(r'(?:sha256:|[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:)[a-f0-9]{64}')
CID = re.compile(r'[a-f0-9]{64}')


async def _finish_control_job(job):
    """Observe the original bounded control call despite repeated cancellation.

    Cancelling a to_thread Task does not stop its Docker subprocess. A second
    cancellation must therefore not abandon that task before its exact container
    identity or cleanup result is known. Callers still propagate cancellation.
    """
    while not job.done():
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            continue
    return job.result()


def validate_container(value):
    fields = {'engine', 'endpoint', 'image', 'uid', 'gid', 'memory_mib', 'cpus',
              'pids_limit', 'tmp_mib', 'home_mib'}
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError('container requires a supported, bounded configuration')
    result = {'uid': max(1, os.getuid()) if hasattr(os, 'getuid') and os.getuid() else 1000,
              'gid': max(1, os.getgid()) if hasattr(os, 'getgid') and os.getgid() else 1000,
              'memory_mib': 1024, 'cpus': 1.0, 'pids_limit': 128, 'tmp_mib': 256, 'home_mib': 256, **value}
    if not isinstance(result.get('engine'), str) or not Path(result['engine']).is_absolute() or '\x00' in result['engine']:
        raise ValueError('container.engine must be an explicit absolute Docker executable')
    endpoint = result.get('endpoint', '')
    if (not isinstance(endpoint, str) or not endpoint.startswith('unix:///') or '\x00' in endpoint
            or '\n' in endpoint or '?' in endpoint or '#' in endpoint):
        raise ValueError('container.endpoint must be an explicit local Unix socket; remote daemons are unsupported')
    if not isinstance(result.get('image'), str) or not IMAGE.fullmatch(result['image']):
        raise ValueError('container.image must be an immutable sha256 ID or digest reference')
    for key, low, high in [('uid', 1, 2147483647), ('gid', 1, 2147483647),
            ('memory_mib', 64, 65536), ('pids_limit', 16, 4096), ('tmp_mib', 16, 4096), ('home_mib', 16, 4096)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f'Invalid container.{key}')
    if type(result['cpus']) not in (int, float) or not math.isfinite(result['cpus']) or not .1 <= result['cpus'] <= 32:
        raise ValueError('container.cpus must be between 0.1 and 32')
    return result


def readiness(config):
    if os.name != 'posix':
        return {'state': 'unavailable', 'code': 'RUNTIME_BACKEND_UNSUPPORTED'}
    if not shutil.which(config['engine']):
        return {'state': 'unavailable', 'code': 'RUNTIME_ENGINE_MISSING'}
    try:
        if not stat.S_ISSOCK(Path(config['endpoint'][7:]).stat().st_mode):
            raise OSError('not a socket')
    except OSError:
        return {'state': 'unavailable', 'code': 'RUNTIME_ENGINE_UNAVAILABLE'}
    return {'state': 'unknown', 'code': 'RUNTIME_ISOLATION_CHECK_AT_START'}


def validate_mount_root(root):
    root = Path(root).resolve(strict=True)
    home = Path.home().resolve()
    if (not root.is_dir() or root == Path(root.anchor) or home == root or home.is_relative_to(root)
            or str(root) in {'/etc', '/usr', '/var', '/private', '/System', '/Library', '/opt', '/root'}
            or ',' in str(root) or '\n' in str(root)):
        raise DevError('RUNTIME_MOUNT_UNSAFE', 'Container runtime requires a dedicated project directory', 403)
    git = root / '.git'
    if git.is_symlink():
        raise DevError('RUNTIME_WORKTREE_UNSUPPORTED', 'Linked Git metadata must be made self-contained before container execution', 409)
    if git.is_file():
        with git.open(encoding='utf-8') as stream:
            text = stream.read(8193).strip()
        if len(text) > 8192:
            raise DevError('RUNTIME_WORKTREE_UNSUPPORTED', 'Git metadata pointer exceeds its inspection budget', 409)
        if text.startswith('gitdir:') and not (root / text[7:].strip()).resolve().is_relative_to(root):
            raise DevError('RUNTIME_WORKTREE_UNSUPPORTED', 'External Git worktree metadata is not mounted into this runtime', 409)
    return root


def inspect_project_mount(root, *, max_entries=200000, max_seconds=8):
    """Reject host IPC/device/hardlink exposures; do not follow project symlinks.

    This is bounded preflight, not a frozen filesystem or an authorization grant.
    The project and Docker daemon remain owner-controlled. Symlink targets are
    resolved in the guest namespace, never followed by this scan.
    """
    root = validate_mount_root(root)
    device = root.stat().st_dev
    deadline, count = time.monotonic() + max_seconds, 0
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > max_entries or time.monotonic() > deadline:
                        raise DevError('RUNTIME_MOUNT_UNVERIFIED', 'Project mount inspection exceeded its budget; no workload was started', 409)
                    info = entry.stat(follow_symlinks=False)
                    if info.st_dev != device:
                        raise DevError('RUNTIME_MOUNT_UNSAFE', 'Project contains a nested filesystem; use a dedicated workspace', 403)
                    if stat.S_ISLNK(info.st_mode):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if os.path.ismount(entry.path):
                            raise DevError('RUNTIME_MOUNT_UNSAFE', 'Nested project mounts are not supported', 403)
                        pending.append(entry.path)
                    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise DevError('RUNTIME_MOUNT_UNSAFE', 'Project contains an IPC/device entry or multiply-linked file; use a self-contained workspace', 403)
    except OSError as exc:
        raise DevError('RUNTIME_MOUNT_UNVERIFIED', 'Project mount inspection was incomplete; no workload was started', 409) from exc
    return {'entries': count, 'scope': 'bounded_prestart_metadata_scan'}


def tmpfs_options(config):
    return {'/tmp': f"rw,noexec,nosuid,nodev,size={config['tmp_mib']}m,mode=1777",
            '/home/project': f"rw,noexec,nosuid,nodev,size={config['home_mib']}m,mode=700,uid={config['uid']},gid={config['gid']}"}


def create_arguments(config, root, plan, name):
    root = validate_mount_root(root)
    relative = plan.cwd.relative_to(root).as_posix()
    workdir = '/workspace' + ('' if relative == '.' else '/' + relative)
    args = ['create', '--name', name, '--pull=never', '--hostname', 'project',
        '--label', 'com.codepier.runtime=' + DRIVER,
        '--label', 'com.codepier.operation=' + name,
        '--network', 'none', '--ipc', 'private', '--cgroupns', 'private',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--user', f"{config['uid']}:{config['gid']}", '--pids-limit', str(config['pids_limit']),
        '--memory', str(config['memory_mib']) + 'm', '--memory-swap', str(config['memory_mib']) + 'm',
        '--cpus', str(config['cpus']), '--restart', 'no', '--no-healthcheck',
        '--mount', f'type=bind,src={root},dst=/workspace,bind-propagation=rprivate,bind-recursive=disabled',
        '--workdir', workdir, '--entrypoint', plan.command[0]]
    for destination, options in tmpfs_options(config).items():
        args += ['--tmpfs', destination + ':' + options]
    for key, value in sorted(plan.environment.items()):
        args += ['--env', key + '=' + value]
    return [*args, config['image'], *plan.command[1:]]


def verify_inspection(value, config, root, plan, name, image_id):
    """Validate actual daemon state, not caller-provided isolation booleans."""
    if not isinstance(value, dict):
        raise DevError('RUNTIME_ISOLATION_UNVERIFIED', 'Container inspection is invalid', 409)
    host, inner = value.get('HostConfig'), value.get('Config')
    if (not isinstance(host, dict) or not isinstance(inner, dict)
            or not isinstance(value.get('State'), dict) or not isinstance(value.get('Mounts'), list)
            or any(not isinstance(item, dict) for item in value['Mounts'])
            or not isinstance(host.get('Mounts'), list) or any(not isinstance(item, dict) for item in host['Mounts'])
            or not isinstance(inner.get('Labels'), dict) or not isinstance(inner.get('Healthcheck'), dict)
            or not isinstance(inner.get('Env'), list)
            or not isinstance(host.get('RestartPolicy'), dict)
            or not isinstance(host['RestartPolicy'].get('Name'), str)
            or any(not isinstance(item.get('Type'), str)
                or not isinstance(item.get('Destination'), str) for item in value['Mounts'])
            or any(not isinstance(host.get(key), list)
                or any(not isinstance(item, str) for item in host[key])
                for key in ('CapDrop', 'SecurityOpt'))):
        raise DevError('RUNTIME_ISOLATION_UNVERIFIED', 'Container inspection structure is incomplete', 409)
    expected = {'Privileged': False, 'ReadonlyRootfs': True, 'NetworkMode': 'none',
        'IpcMode': 'private', 'PidMode': '', 'UTSMode': '', 'CgroupnsMode': 'private',
        'PidsLimit': config['pids_limit'], 'Memory': config['memory_mib'] * 1024 * 1024,
        'MemorySwap': config['memory_mib'] * 1024 * 1024, 'NanoCpus': round(config['cpus'] * 1_000_000_000)}
    valid = all(type(host.get(key)) is type(item) and host[key] == item
                for key, item in expected.items())
    valid = valid and not any(host.get(key) for key in ('CapAdd', 'Devices', 'DeviceRequests',
        'Links', 'PortBindings', 'PublishAllPorts', 'ExtraHosts', 'VolumesFrom', 'Binds'))
    valid = valid and set(host.get('CapDrop') or []) == {'ALL'}
    security = set(host.get('SecurityOpt') or [])
    valid = valid and bool(security & {'no-new-privileges', 'no-new-privileges=true'})
    valid = valid and not any('unconfined' in item for item in security)
    valid = valid and host.get('Tmpfs') == tmpfs_options(config)
    valid = valid and host.get('RestartPolicy', {}).get('Name') in {'no', ''}
    valid = valid and inner.get('User') == f"{config['uid']}:{config['gid']}"
    valid = valid and inner.get('Entrypoint') == [plan.command[0]] and (inner.get('Cmd') or []) == plan.command[1:]
    relative = plan.cwd.relative_to(root).as_posix()
    valid = valid and inner.get('WorkingDir') == '/workspace' + ('' if relative == '.' else '/' + relative)
    valid = valid and inner.get('Labels', {}).get('com.codepier.operation') == name
    valid = valid and inner.get('Labels', {}).get('com.codepier.runtime') == DRIVER
    valid = valid and value.get('Image') == image_id and value['State'].get('Running') is False
    valid = valid and inner.get('Healthcheck', {}).get('Test') == ['NONE']
    actual_env = {}
    for item in inner['Env']:
        if not isinstance(item, str) or '=' not in item or '\x00' in item:
            raise DevError('RUNTIME_ISOLATION_UNVERIFIED', 'Container environment is invalid', 409)
        key, env_value = item.split('=', 1)
        if not key or key in actual_env:
            raise DevError('RUNTIME_ISOLATION_UNVERIFIED', 'Container environment is ambiguous', 409)
        actual_env[key] = env_value
    valid = valid and all(actual_env.get(key) == item for key, item in plan.environment.items())
    mounts = value.get('Mounts', [])
    binds = [item for item in mounts if item.get('Type') == 'bind']
    valid = valid and len(binds) == 1 and all(item.get('Type') in {'bind', 'tmpfs'} for item in mounts)
    if binds:
        bind = binds[0]
        valid = valid and bind.get('Source') == str(root) and bind.get('Destination') == '/workspace'
        valid = valid and bind.get('RW') is True and bind.get('Propagation') == 'rprivate'
    specs = host.get('Mounts', [])
    valid = valid and len(specs) == 1 and isinstance(specs[0].get('BindOptions'), dict) and specs[0]['BindOptions'].get('NonRecursive') is True
    valid = valid and all(item.get('Destination') in {'/workspace', '/tmp', '/home/project'} for item in mounts)
    if not valid:
        raise DevError('RUNTIME_ISOLATION_UNVERIFIED', 'Created container does not match the mandatory runtime boundary; project code was not started', 409)
    return {'state': 'verified_for_operation', 'driver': DRIVER,
            'evidence_sha256': hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(),
            'filesystem_scope': 'workspace_bind', 'network': 'none', 'host_credentials_inherited': False}


class ContainerRuntime:
    def __init__(self, config, root, plan, operation_id):
        self.config = plan.profile['container']
        self.root, self.plan = Path(root).resolve(), plan
        self.name = 'cp-runtime-' + hashlib.sha256(str(operation_id).encode()).hexdigest()[:32]
        self.directory = Path(config['state_dir']).resolve() / 'runtime-containers'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise DevError('RUNTIME_DIRECTORY_UNSAFE', 'Container control state cannot use links', 409)
        self.receipt = self.directory / (self.name + '.json')
        if self.receipt.exists():
            raise DevError('RUNTIME_OPERATION_REQUIRES_RECOVERY', 'This runtime operation already has a control receipt; do not replay', 409)
        self.client_env = {'PATH': os.defpath, 'HOME': str(self.directory), 'DOCKER_CONFIG': str(self.directory / 'client')}
        self.identifier = None
        self.creation_attempted = False
        self.cleanup_attempted = False
        self.evidence = None
        self.workload_outcome = None
        self.cleanup_state = {'state': 'not_created', 'pending': False}

    def _control(self, args, *, json_result=False):
        try:
            result = run_control([self.config['engine'], '--host', self.config['endpoint'], *args], self.client_env)
        except OSError as exc:
            raise DevError('RUNTIME_ENGINE_UNAVAILABLE', 'Configured container engine cannot be started', 503) from exc
        if result.returncode:
            raise DevError('RUNTIME_ENGINE_FAILED', 'Container control failed; no native fallback is allowed', 409)
        if json_result:
            try:
                return json.loads(result.stdout)
            except (ValueError, UnicodeError) as exc:
                raise DevError('RUNTIME_CONTROL_INVALID', 'Container engine returned invalid inspection data', 409) from exc
        try:
            return result.stdout.decode('utf-8', errors='strict').strip()
        except UnicodeError as exc:
            raise DevError('RUNTIME_CONTROL_INVALID', 'Container control returned invalid UTF-8', 409) from exc

    def _record(self, state):
        atomic_json(self.receipt, {'schema_version': 1, 'driver': DRIVER, 'name': self.name,
            'container_id': self.identifier, 'state': state, 'runtime_ref': self.plan.runtime['runtime_ref'],
            'revision': self.plan.runtime['revision'], 'cleanup': self.cleanup_state,
            'workload_outcome': self.workload_outcome})

    def prepare(self):
        status = readiness(self.config)
        if status['state'] == 'unavailable':
            raise DevError(status['code'], 'The configured isolated runtime is unavailable; no native fallback is allowed', 503)
        inspect_project_mount(self.root)
        info = self._control(['info', '--format', '{{json .}}'], json_result=True)
        if (not isinstance(info, dict) or info.get('OSType') != 'linux'
                or not isinstance(info.get('SecurityOptions'), list)
                or any(not isinstance(item, str) for item in info['SecurityOptions'])
                or not any('name=seccomp' in item.split(',') for item in info['SecurityOptions'])):
            raise DevError('RUNTIME_ENGINE_UNSUPPORTED', 'The runtime requires a Linux engine with seccomp', 409)
        images = self._control(['image', 'inspect', self.config['image']], json_result=True)
        if (not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict)
                or not isinstance(images[0].get('Config'), dict) or images[0]['Config'].get('Volumes')):
            raise DevError('RUNTIME_IMAGE_UNSUPPORTED', 'The approved image must exist and must not declare extra volumes', 409)
        image_id = images[0].get('Id', '')
        if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
            raise DevError('RUNTIME_IMAGE_UNSUPPORTED', 'Image identity is not immutable', 409)
        self._record('creating')
        self.creation_attempted = True
        try:
            identifier = self._control(create_arguments(self.config, self.root, self.plan, self.name))
            if not CID.fullmatch(identifier):
                raise DevError('RUNTIME_CONTROL_INVALID', 'Container creation identity is unconfirmed; do not replay', 409)
            self.identifier = identifier
            self.cleanup_state = {'state': 'required', 'pending': True}
            self._record('created')
            values = self._control(['inspect', identifier], json_result=True)
            if (not isinstance(values, list) or len(values) != 1
                    or not isinstance(values[0], dict) or values[0].get('Id') != identifier):
                raise DevError('RUNTIME_ISOLATION_UNVERIFIED', 'Container inspection identity changed', 409)
            self.evidence = verify_inspection(values[0], self.config, self.root, self.plan, self.name, image_id)
            self._record('verified_before_start')
        except BaseException as exc:
            self.cleanup()
            if self.cleanup_state['pending'] and isinstance(exc, Exception):
                raise DevError('RUNTIME_CREATION_UNCERTAIN', 'Runtime creation or cleanup is unconfirmed; inspect the original operation, do not replay', 409) from exc
            raise
        return [self.config['engine'], '--host', self.config['endpoint'], 'start', '--attach', self.identifier]

    def outcome(self):
        """Read the real workload state; a Docker client exit is not job success."""
        result = {'state': 'unconfirmed', 'source': 'container_inspection', 'workload_exit_code': None}
        try:
            rows = self._control(['inspect', self.identifier], json_result=True)
            if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('Id') != self.identifier:
                raise ValueError('identity changed')
            state = rows[0].get('State', {})
            start_value = state.get('StartedAt')
            started = isinstance(start_value, str) and bool(re.fullmatch(r'[1-9][0-9]{3}-[0-9]{2}-[0-9]{2}T[^\s]+', start_value))
            if state.get('Status') == 'exited' and state.get('Running') is False and started and type(state.get('ExitCode')) is int:
                result.update(state='confirmed', workload_exit_code=state['ExitCode'], oom_killed=state.get('OOMKilled') is True)
            elif state.get('Status') == 'created' and state.get('Running') is False and not started:
                result['state'] = 'not_started'
        except (DevError, ValueError, TypeError, AttributeError):
            pass
        self.workload_outcome = result
        return result

    async def outcome_async(self):
        job = asyncio.create_task(asyncio.to_thread(self.outcome))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            await _finish_control_job(job)
            raise

    def cleanup(self):
        # A full immutable ID returned by our create, not a guessed name or PID.
        if self.cleanup_attempted:
            return self.cleanup_state
        self.cleanup_attempted = True
        if self.identifier is None:
            if self.creation_attempted:
                self.cleanup_state = {'state': 'creation_unconfirmed', 'pending': True, 'code': 'RUNTIME_CREATION_UNCERTAIN'}
                try:
                    self._record('needs_review')
                except OSError:
                    pass  # Still unconfirmed; no successful cleanup is claimed.
            return self.cleanup_state
        try:
            self._control(['rm', '--force', self.identifier])
            self.cleanup_state = {'state': 'removed', 'pending': False}
        except (DevError, OSError):
            self.cleanup_state = {'state': 'unconfirmed', 'pending': True, 'code': 'RUNTIME_CLEANUP_PENDING'}
        try:
            self._record('finished')
        except OSError:
            self.cleanup_state = {'state': 'record_unconfirmed', 'pending': True, 'code': 'RUNTIME_CLEANUP_PENDING'}
        return self.cleanup_state

    async def prepare_async(self):
        job = asyncio.create_task(asyncio.to_thread(self.prepare))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            try:
                await _finish_control_job(job)
            finally:
                await self.cleanup_async()
            raise

    async def cleanup_async(self):
        job = asyncio.create_task(asyncio.to_thread(self.cleanup))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            await _finish_control_job(job)
            raise
