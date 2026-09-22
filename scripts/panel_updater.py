#!/usr/bin/env python3
"""Restricted host updater: persisted operations over a filesystem-protected Unix socket.

Install is an explicit host-administrator action. Serving never accepts shell
commands, arbitrary URLs, paths, Docker options, credentials or Compose files.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import shutil
import socketserver
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit
import uuid

try:
    from .panel_update_source import DEFAULT_REPOSITORY, UpdateError, fetch, latest_release, repository_name, unpack_bundle, version_tuple
    from .panel_update_runtime import DockerRuntime, atomic_bytes, atomic_json, data_volume, fingerprint, load_compose_config, read_json, run
except ImportError:
    from panel_update_source import DEFAULT_REPOSITORY, UpdateError, fetch, latest_release, repository_name, unpack_bundle, version_tuple
    from panel_update_runtime import DockerRuntime, atomic_bytes, atomic_json, data_volume, fingerprint, load_compose_config, read_json, run

TERMINAL = {'succeeded', 'failed', 'rolled_back', 'recovery_required'}
CUTOVER_PHASES = {'stopping', 'copying', 'starting', 'verifying', 'committing', 'committed', 'rolling_back'}
PUBLIC_FIELDS = {'id', 'kind', 'state', 'phase', 'message', 'created', 'updated', 'from_version',
                 'target_version', 'request_key', 'error', 'events', 'backup', 'commit_decided'}


def job_view(job):
    return {key: copy.deepcopy(value) for key, value in (job or {}).items() if key in PUBLIC_FIELDS}


def key_digest(key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', key):
        raise UpdateError('INVALID_KEY', '更新请求需要有效的幂等键')
    return hashlib.sha256(key.encode()).hexdigest()


@contextmanager
def admission_lock(home):
    """Fence daemon submissions while a host administrator replaces the service."""
    fd = os.open(Path(home) / 'admission.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateError('UPDATER_INSTALLING', '更新服务正在接收其他请求或进行宿主机配置，请稍后重试', 409) from exc
        yield
    finally:
        os.close(fd)


def secure_host_path(path):
    """A root service must not execute code below a writable unprivileged parent."""
    path = Path(path).resolve()
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise UpdateError('UNSAFE_HOST_PATH', 'root 更新服务要求部署目录及其父目录由 root 拥有，且组/其他用户不可写；建议使用 /opt/codepier')


class Manager:
    def __init__(self, root, *, runtime=None, start_workers=True):
        self.root = Path(root).resolve()
        self.home = self.root / '.codepier-updater'
        self.config = read_json(self.home / 'config.json')
        if not self.config:
            raise UpdateError('UPDATER_NOT_INSTALLED', '请先在宿主机安装面板更新服务')
        self.runtime = runtime or DockerRuntime(self.root, self.home, self.config)
        self.lock = threading.RLock()
        self.start_workers = start_workers
        self.worker = None
        for name in ('jobs', 'keys', 'releases', 'run'):
            (self.home / name).mkdir(mode=0o700, exist_ok=True)

    def current_job(self):
        latest = read_json(self.home / 'latest.json', {})
        identifier = latest.get('id', '')
        if identifier and re.fullmatch(r'[a-f0-9]{32}', identifier):
            return read_json(self.home / 'jobs' / identifier / 'status.json')
        return None

    def status(self, request_key=''):
        with self.lock:
            job = self.current_job()
            receipt = None
            if request_key:
                receipt = read_json(self.home / 'keys' / (key_digest(request_key) + '.json'))
                if receipt:
                    job = read_json(self.home / 'jobs' / receipt['id'] / 'status.json')
            candidate = read_json(self.home / 'candidate.json')
            current = read_json(self.home / 'current.json', {})
            version = current.get('version', '')
            available = bool(candidate and time.time() - candidate['checked_at'] <= 900 and
                             version_tuple(candidate['version']) > version_tuple(version))
            active = self.current_job()
            return {'enabled': True, 'repository': self.config['repository'], 'current_version': version,
                    'candidate': candidate, 'update_available': available,
                    'busy': bool(active and active['state'] not in TERMINAL),
                    'recovery_required': bool(active and active['state'] == 'recovery_required'),
                    'operation': job_view(job) if job else None,
                    'request_found': bool(receipt) if request_key else None}

    def save(self, job, *, phase=None, message=None, state=None, **values):
        with self.lock:
            job.update(values)
            if phase is not None:
                job['phase'] = phase
            if message is not None:
                job['message'] = message
                job.setdefault('events', []).append({'at': time.time(), 'phase': job['phase'], 'message': message})
                job['events'] = job['events'][-40:]
            if state is not None:
                job['state'] = state
            job['updated'] = time.time()
            atomic_json(self.home / 'jobs' / job['id'] / 'status.json', job)

    def submit(self, kind, body):
        allowed = {'idempotency_key', 'current_version', 'actor'} | ({'version', 'release_id', 'sha256'} if kind == 'apply' else set())
        if kind not in {'check', 'apply'} or not isinstance(body, dict) or set(body) != allowed:
            raise UpdateError('INVALID_REQUEST', '更新服务只接受检查或已确认的正式版本更新')
        key = body['idempotency_key']
        digest = key_digest(key)
        version_tuple(body['current_version'])
        if not isinstance(body['actor'], str) or not 1 <= len(body['actor']) <= 200:
            raise UpdateError('INVALID_REQUEST', '更新请求缺少管理员审计身份')
        content_hash = hashlib.sha256(json.dumps({'kind': kind, 'body': body}, sort_keys=True).encode()).hexdigest()
        with admission_lock(self.home), self.lock:
            receipt = read_json(self.home / 'keys' / (digest + '.json'))
            if receipt:
                if receipt['fingerprint'] != content_hash:
                    raise UpdateError('IDEMPOTENCY_CONFLICT', '同一个更新请求编号不能用于不同操作', 409)
                return {'operation': job_view(read_json(self.home / 'jobs' / receipt['id'] / 'status.json')), 'replayed': True}
            latest = self.current_job()
            if latest and (latest['state'] not in TERMINAL or latest['state'] == 'recovery_required'):
                raise UpdateError('UPDATE_BUSY', '已有更新操作或待恢复状态，请先查看当前进度', 409)
            if len(list((self.home / 'jobs').iterdir())) >= 1000:
                raise UpdateError('UPDATE_QUOTA', '更新记录已满，请在宿主机归档历史记录后再试', 409)
            current = read_json(self.home / 'current.json')
            if body['current_version'] != current['version']:
                raise UpdateError('VERSION_CHANGED', 'Hub 与更新服务记录的版本不一致，请在宿主机重新检查配置', 409)
            candidate = read_json(self.home / 'candidate.json')
            if kind == 'apply':
                if not candidate or time.time() - candidate['checked_at'] > 900:
                    raise UpdateError('RELEASE_EXPIRED', '版本检查已过期，请重新检查 GitHub 更新', 409)
                if (body['version'] != candidate['version'] or type(body['release_id']) is not int or
                        body['release_id'] != candidate['release_id'] or body['sha256'] != candidate['sha256']):
                    raise UpdateError('RELEASE_CHANGED', '已确认的版本或 SHA-256 已变化，请重新检查', 409)
                if version_tuple(candidate['version']) <= version_tuple(current['version']):
                    raise UpdateError('NO_UPDATE', '当前没有更新版本；不自动降级或重新部署同版本', 409)
            job = {'id': uuid.uuid4().hex, 'kind': kind, 'state': 'queued', 'phase': 'queued',
                   'message': '更新请求已持久保存', 'request_key': key, 'actor': body['actor'],
                   'created': time.time(), 'updated': time.time(), 'from_version': current['version'],
                   'target_version': candidate['version'] if kind == 'apply' else '', 'events': []}
            path = self.home / 'jobs' / job['id']
            path.mkdir(mode=0o700)
            atomic_json(path / 'current.before.json', current)
            if kind == 'apply':
                atomic_json(path / 'release.json', candidate)
            self.save(job)
            atomic_json(self.home / 'keys' / (digest + '.json'), {'id': job['id'], 'fingerprint': content_hash})
            atomic_json(self.home / 'latest.json', {'id': job['id']})
            if self.start_workers:
                self.worker = threading.Thread(target=self.perform, args=(job,), daemon=True, name='codepier-update')
                self.worker.start()
            return {'operation': job_view(job), 'replayed': False}

    def acquire_install_lock(self, job):
        lock = self.root / '.codepier-install.lock'
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise UpdateError('INSTALL_BUSY', '已有宿主机安装或更新锁，未开始第二次部署', 409) from exc
        atomic_json(lock / 'panel-update-owner.json', {'id': job['id']})

    def release_install_lock(self, job):
        lock = self.root / '.codepier-install.lock'
        if lock.is_symlink():
            return
        owner = read_json(lock / 'panel-update-owner.json', {})
        if owner.get('id') == job['id']:
            (lock / 'panel-update-owner.json').unlink()
            lock.rmdir()

    def perform(self, job):
        try:
            if job['kind'] == 'apply':
                self.acquire_install_lock(job)
            self.save(job, state='running', phase='checking', message='正在检查 GitHub 正式 Release')
            if job['kind'] == 'check':
                cached = read_json(self.home / 'candidate.json')
                # Repeated explicit checks reuse a short positive cache; no hidden polling of GitHub.
                candidate = cached if cached and 0 <= time.time() - cached['checked_at'] < 60 else latest_release(self.config['repository'])
                atomic_json(self.home / 'candidate.json', candidate)
                newer = version_tuple(candidate['version']) > version_tuple(job['from_version'])
                self.save(job, state='succeeded', phase='checked', target_version=candidate['version'],
                          message='发现新的正式版本，可一键更新面板及 Agent 文件' if newer else '已是最新版本，或本机版本比正式 Release 更新')
                return
            release = read_json(self.home / 'jobs' / job['id'] / 'release.json')
            fresh = latest_release(self.config['repository'])
            if any(fresh[key] != release[key] for key in ('release_id', 'asset_id', 'version', 'sha256', 'bytes', 'url')):
                raise UpdateError('RELEASE_CHANGED', 'GitHub Release 在确认后发生变化；未安装其他版本，请重新检查', 409)
            if shutil.disk_usage(self.home).free < 512 * 1024 * 1024:
                raise UpdateError('DISK_SPACE', '宿主机可用空间不足 512 MiB，拒绝开始更新', 409)
            directory = self.home / 'releases' / job['id']
            directory.mkdir(mode=0o700)
            archive = directory / 'source.zip'
            self.save(job, phase='downloading', message='正在下载源码包并核对 GitHub SHA-256')
            fetch(release['url'], limit=release['bytes'], destination=archive, deadline_seconds=300)
            source = unpack_bundle(archive, directory / 'source', release)
            self.save(job, phase='building', message='正在构建独立候选镜像；现有面板仍正常服务', **self.runtime.prepare(job, source))
            package = self.runtime.build(job, source)
            self.save(job, phase='quiescing', message='候选镜像和 Agent 包已验证，正在等待操作结束', package=package)
            self.runtime.quiesce(job)
            self.save(job, phase='stopping', message='维护保护已启用，正在停止旧 Hub')
            self.runtime.stop(job)
            self.save(job, phase='copying', message='正在复制并校验完整数据卷；原数据保留为回退备份',
                      backup={'volume': job['old_volume'], 'retained': True})
            copied = self.runtime.copy_data(job)
            self.save(job, phase='starting', message='数据副本校验通过，正在启动新版本', copy_summary=copied)
            self.runtime.start(job, 'target')
            self.save(job, phase='verifying', message='正在校验新 Hub 健康状态、版本及 Agent 下载文件')
            self.runtime.verify(job)
            self.save(job, phase='committing', message='新版本验证通过，正在持久保存部署配置')
            self.runtime.commit(job)
            # Irreversible decision is durable BEFORE admitting any new traffic.
            self.save(job, phase='committed', message='新版本已提交，正在恢复面板连接', commit_decided=True)
            self.runtime.clear_gate()
            self.save(job, state='succeeded', phase='done', message='面板、Hub 和配套 Agent 文件更新成功；已连接 Agent 未被强制重启')
            self.release_install_lock(job)
        except Exception as exc:
            self.fail(job, exc)

    def fail(self, job, exc):
        if not isinstance(exc, UpdateError):
            try:
                detail = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-65536:]
                atomic_bytes(self.home / 'jobs' / job['id'] / 'error.log', detail.encode('utf-8'))
            except OSError:
                pass  # Lack of log space must never prevent recovery.
        error = {'code': exc.code, 'message': exc.message} if isinstance(exc, UpdateError) else {
            'code': 'UPDATE_FAILED', 'message': '更新出现异常，请检查宿主机更新器日志；不会自动重放更新命令'}
        try:
            if job.get('commit_decided'):
                # New requests may have been acknowledged. NEVER restore old data now.
                self.save(job, state='recovery_required', error=error, message='新版本已经提交，禁止回退旧数据；需要宿主机检查')
            elif job['phase'] in CUTOVER_PHASES:
                self.save(job, phase='rolling_back', message='切换未完成，正在恢复原镜像和原数据卷', error=error)
                self.runtime.rollback(job)
                self.save(job, state='rolled_back', phase='rolled_back', message='更新失败，原面板及原数据卷已恢复；候选文件保留供排查')
            else:
                if job['phase'] == 'quiescing':
                    self.runtime.clear_gate()
                self.save(job, state='failed', error=error, message=error['message'])
            if job['state'] != 'recovery_required':
                self.release_install_lock(job)
        except Exception:
            self.save(job, state='recovery_required', error=error, message='自动恢复未完成；保护和备份保留，请在宿主机人工检查')

    def recover(self, *, manual=False):
        # Durable accepted-but-not-started records are NOT automatically replayed.
        for path in sorted((self.home / 'jobs').glob('*/status.json')):
            job = read_json(path)
            if job['state'] in TERMINAL and not (manual and job['state'] == 'recovery_required'):
                if job['state'] != 'recovery_required':
                    self.release_install_lock(job)
                continue
            if job.get('commit_decided'):
                try:
                    self.runtime.set_gate(job)
                    self.runtime.verify(job)
                    self.runtime.clear_gate()
                    self.save(job, state='succeeded', phase='done', message='已核验并恢复已提交的新版本；未回退旧数据')
                    self.release_install_lock(job)
                except Exception as exc:
                    self.fail(job, exc)
            else:
                self.fail(job, UpdateError('UPDATER_INTERRUPTED', '更新服务曾中断；没有自动重放下载或部署命令', 500))


class Handler(BaseHTTPRequestHandler):
    server_version = 'CodePierUpdater/1'
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        pass  # Never put headers, query strings or administrator identities into system logs.

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def respond(self, status, data):
        content = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def handle_request(self, write=False):
        try:
            parsed = urlsplit(self.path)
            if not write and parsed.path == '/status':
                query = parse_qs(parsed.query, strict_parsing=True) if parsed.query else {}
                if set(query) - {'request_key'} or len(query.get('request_key', [])) > 1:
                    raise UpdateError('INVALID_REQUEST', '无效的状态查询')
                return self.respond(200, self.server.manager.status(query.get('request_key', [''])[0]))
            if write and self.path in {'/check', '/apply'}:
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 16384:
                    raise UpdateError('INVALID_REQUEST', '更新请求超过大小限制')
                body = json.loads(self.rfile.read(int(length)))
                return self.respond(202, self.server.manager.submit(self.path[1:], body))
            raise UpdateError('NOT_FOUND', '更新服务不提供此操作', 404)
        except UpdateError as exc:
            self.respond(exc.status, {'error': {'code': exc.code, 'message': exc.message}})
        except (ValueError, UnicodeError, TypeError):
            self.respond(400, {'error': {'code': 'INVALID_REQUEST', 'message': '更新请求格式无效'}})
        except Exception:
            self.respond(500, {'error': {'code': 'UPDATER_ERROR', 'message': '更新服务发生错误，请检查宿主机服务'}})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(True)


class Server(socketserver.UnixStreamServer):
    allow_reuse_address = False


def acquire_lock(home):
    descriptor = os.open(Path(home) / 'service.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise UpdateError('UPDATER_BUSY', '更新服务已运行；不要并行启动第二个更新进程', 409) from exc
    return descriptor


def serve(root):
    manager = Manager(root)
    descriptor = acquire_lock(manager.home)
    try:
        manager.recover()
        socket = manager.home / 'run' / 'updater.sock'
        if socket.is_symlink():
            raise UpdateError('UNSAFE_SOCKET', '更新套接字路径不安全')
        socket.unlink(missing_ok=True)
        with Server(str(socket), Handler) as server:
            server.manager = manager
            os.chown(socket, 0, 10001)
            socket.chmod(0o660)
            server.serve_forever(poll_interval=0.5)
    finally:
        os.close(descriptor)


def unit_name(root):
    return 'codepier-panel-updater-' + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12] + '.service'


def systemd_quote(value):
    if any(ord(c) < 32 or ord(c) == 127 for c in str(value)):
        raise UpdateError('INVALID_PATH', '服务路径包含无效字符')
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def install(root, repository):
    if sys.platform != 'linux' or os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        raise UpdateError('HOST_SETUP_REQUIRED', '请在 Linux/systemd 宿主机以 root 安装更新服务')
    root = Path(root).resolve()
    secure_host_path(root)
    secure_host_path(Path(sys.executable).resolve())
    home = root / '.codepier-updater'
    if home.is_symlink():
        raise UpdateError('INVALID_INSTALLATION', '更新服务目录不能是符号链接')
    home.mkdir(mode=0o700, exist_ok=True)
    with admission_lock(home):
        _install_locked(root, repository)


def _install_locked(root, repository):
    if sys.platform != 'linux' or os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        raise UpdateError('HOST_SETUP_REQUIRED', '请在 Linux/systemd 宿主机以 root 安装更新服务；不要在 Hub 容器内运行')
    root = Path(root).resolve()
    repository = repository_name(repository)
    home = root / '.codepier-updater'
    if home.is_symlink() or (root / '.env').is_symlink() or not (root / '.env').is_file():
        raise UpdateError('INVALID_INSTALLATION', '需要已有的完整部署目录和非符号链接 .env')
    if len(str(home / 'run/updater.sock').encode()) > 100:
        raise UpdateError('PATH_TOO_LONG', '部署路径太长，无法建立 Unix 更新套接字')
    home.mkdir(mode=0o700, exist_ok=True)
    for name in ('service', 'run', 'jobs', 'keys', 'releases'):
        path = home / name
        if path.is_symlink():
            raise UpdateError('INVALID_INSTALLATION', '更新服务目录不能是符号链接')
        path.mkdir(mode=0o700, exist_ok=True)
    pending = read_json(home / 'latest.json', {})
    if pending:
        previous = read_json(home / 'jobs' / pending['id'] / 'status.json')
        if previous and (previous['state'] not in TERMINAL or previous['state'] == 'recovery_required'):
            raise UpdateError('UPDATER_BUSY', '已有未结束更新，不能覆盖更新服务；请先核查或恢复', 409)
    ids = run(['docker', 'compose', 'ps', '-q', 'hub'], cwd=root).split()
    if len(ids) != 1:
        raise UpdateError('INVALID_INSTALLATION', '安装更新服务前需要一个已运行的 Hub')
    container = json.loads(run(['docker', 'inspect', ids[0]], cwd=root))[0]
    selected = container.get('Config', {}).get('Labels', {}).get('com.docker.compose.project.config_files', '')
    # On updater-managed containers the label points at a frozen job snapshot.
    previous_config = read_json(home / 'current.json', {})
    paths = [root / name for name in previous_config.get('files', {}) if name != '.env'] if previous_config else []
    if not paths:
        paths = [Path(value) for value in selected.split(',')] if selected else [root / 'compose.yml']
    if any(not path.is_file() or path.is_symlink() or root not in path.resolve().parents for path in paths):
        raise UpdateError('UNSUPPORTED_DEPLOYMENT', 'Compose 覆盖文件必须位于当前部署目录内')
    command = ['docker', 'compose']
    for path in paths:
        command.extend(['-f', str(path)])
    spec = load_compose_config(run(command + ['config', '--format', 'json'], cwd=root))
    data_volume(spec)
    project = spec.get('name', '')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', project):
        raise UpdateError('INVALID_INSTALLATION', 'Compose 项目名称无效')
    mounts = spec['services']['hub'].get('volumes', [])
    expected = [v for v in mounts if v.get('target') == '/run/codepier-updater']
    if (len(expected) != 1 or expected[0].get('type') != 'bind' or expected[0].get('read_only') is not True or
            Path(expected[0].get('source', '')).resolve() != (home / 'run').resolve()):
        raise UpdateError('INVALID_INSTALLATION', '请先使用包含只读更新套接字挂载的新版 Compose 配置')
    unit = unit_name(root)
    service_path = Path('/etc/systemd/system') / unit
    # The cross-process admission lock prevents a job racing this idle check.
    if service_path.exists():
        run(['systemctl', 'stop', unit], cwd=root)
    descriptor = acquire_lock(home)
    try:
        home.chmod(0o700)
        (home / 'run').chmod(0o755)
        for name in ('panel_updater.py', 'panel_update_runtime.py', 'panel_update_source.py'):
            source = Path(__file__).resolve().parent / name
            atomic_bytes(home / 'service' / name, source.read_bytes())
        from_version = json.loads(run(['docker', 'exec', ids[0], 'python', '-c',
                                     "import json; from shared.util import VERSION; print(json.dumps(VERSION))"], cwd=root))
        version_tuple(from_version)
        files = {str(p.relative_to(root)): fingerprint(p) for p in paths}
        files['.env'] = fingerprint(root / '.env')
        config = {'root': str(root), 'repository': repository, 'project': project, 'unit': unit, 'protocol': 1}
        old_config = read_json(home / 'config.json', {})
        if old_config.get('repository') != repository:
            (home / 'candidate.json').unlink(missing_ok=True)
        atomic_json(home / 'config.json', config)
        atomic_json(home / 'current.json', {'compose': spec, 'files': files, 'version': from_version})
        executable = Path(sys.executable).resolve()
        text = ('[Unit]\nDescription=CodePier panel update service\nAfter=docker.service network-online.target\nWants=network-online.target\n\n'
                '[Service]\nType=simple\nUser=root\nUMask=0077\nWorkingDirectory=' + systemd_quote(root) + '\n'
                'ExecStart=' + systemd_quote(executable) + ' ' + systemd_quote(home / 'service/panel_updater.py') + ' serve --root ' + systemd_quote(root) + '\n'
                'Restart=on-failure\nRestartSec=5\nKillMode=control-group\nTimeoutStopSec=15\nNoNewPrivileges=true\nPrivateTmp=true\n'
                '\n[Install]\nWantedBy=multi-user.target\n')
        atomic_bytes(service_path, text.encode(), 0o644)
    finally:
        os.close(descriptor)
    run(['systemctl', 'daemon-reload'], cwd=root)
    run(['systemctl', 'enable', '--now', unit], cwd=root)
    run(['systemctl', 'is-active', '--quiet', unit], cwd=root)
    # Verify the read-only socket bind is actually reachable as the Hub user.
    probe = "import socket,json; s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect('/run/codepier-updater/updater.sock'); s.sendall(b'GET /status HTTP/1.0\\r\\n\\r\\n'); assert b'200' in s.recv(256).split(b'\\r\\n',1)[0]; s.close()"
    for attempt in range(10):
        try:
            run(['docker', 'exec', ids[0], 'python', '-c', probe], cwd=root, timeout=5)
            break
        except UpdateError:
            if attempt == 9:
                raise UpdateError('UPDATER_SOCKET_UNREACHABLE', '更新服务已启动，但 Hub 无法访问更新套接字；请核对 UID 10001、只读挂载及 Docker userns 配置', 500)
            time.sleep(1)
    print('面板更新服务已安装：' + unit)
    print('入口：系统设置 → 面板更新。仅使用固定 GitHub 正式 Release；不会自动更新在线 Agent。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'serve', 'status', 'recover'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repository', default=DEFAULT_REPOSITORY)
    args = parser.parse_args()
    try:
        if args.action == 'install':
            install(args.root, args.repository)
        elif args.action == 'serve':
            serve(args.root)
        else:
            manager = Manager(args.root)
            if args.action == 'recover':
                descriptor = acquire_lock(manager.home)
                try:
                    manager.recover(manual=True)
                finally:
                    os.close(descriptor)
            print(json.dumps(manager.status(), ensure_ascii=False, indent=2))
    except UpdateError as exc:
        print(exc.code + ': ' + exc.message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
