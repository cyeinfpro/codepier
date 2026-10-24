"""Host-only Docker cutover primitives. No downloaded Compose or installer is run."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

try:
    from .panel_update_source import UpdateError
except ImportError:
    from panel_update_source import UpdateError


def atomic_bytes(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value, mode=0o600):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'), mode)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path, default=None):
    try:
        if Path(path).is_symlink() or Path(path).stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Invalid state file')
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def fingerprint(path):
    if Path(path).is_symlink():
        raise UpdateError('DEPLOYMENT_CHANGED', '部署文件是符号链接，请先在宿主机检查')
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(command, *, cwd, timeout=120, log=None):
    """Bound time/output and kill the whole local subprocess group on failure."""
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline or os.fstat(output.fileno()).st_size > 16 * 1024 * 1024:
                    raise UpdateError('COMMAND_LIMIT', '更新命令超时或输出超限；请检查宿主机日志', 500)
                time.sleep(0.1)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
        output.seek(0)
        raw = output.read(16 * 1024 * 1024 + 1)
    if log is not None:
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(raw[-1024 * 1024:] + b'\n')
    if process.returncode != 0 or len(raw) > 16 * 1024 * 1024:
        raise UpdateError('COMMAND_FAILED', '更新命令失败；详细输出已保存在宿主机任务日志' if log is not None else '宿主机命令失败，请检查 Docker Compose/systemd 及相关服务状态', 500)
    return raw.decode('utf-8', errors='replace').strip()


def load_compose_config(raw):
    """Decode the round-trip dollar escaping emitted by `docker compose config`.

    Compose escapes all dollar signs in its JSON/YAML output after resolving
    interpolation. Keep an unescaped internal model so save_compose escapes
    exactly once, including values such as passwords and variable-like text.
    """
    return json.loads(raw.replace('$$', '$'))


def save_compose(path, spec):
    """Compose interpolates JSON too: preserve literal dollars in trusted values."""
    def escaped(value):
        if isinstance(value, str):
            return value.replace('$', '$$')
        if isinstance(value, list):
            return [escaped(item) for item in value]
        if isinstance(value, dict):
            return {key: escaped(item) for key, item in value.items()}
        return value
    atomic_json(Path(path).with_suffix('.spec.json'), spec)
    atomic_json(path, escaped(spec))


def storage_layout(spec):
    service = spec.get('services', {}).get('hub', {})
    environment = service.get('environment', {})
    configured = environment.get('HUB_STATE_DIR', '')
    if not configured:
        return '/app/data', 'CODEPIER_HUB_DATA_VOLUME'
    if configured != '/app/state' or environment.get('HUB_DATA_DIR') != '/app/data':
        raise UpdateError('UNSUPPORTED_DEPLOYMENT', '分离存储需要固定 /app/state 与 /app/data 挂载')
    assets = [v for v in service.get('volumes', []) if isinstance(v, dict) and v.get('target') == '/app/data']
    if len(assets) != 1 or assets[0].get('type') != 'volume':
        raise UpdateError('UNSUPPORTED_DEPLOYMENT', '附件必须保留独立稳定命名卷')
    value = spec.get('volumes', {}).get(assets[0].get('source'), {})
    if not value.get('external') or not value.get('name'):
        raise UpdateError('UNSUPPORTED_DEPLOYMENT', '附件卷必须外部管理并禁止历史清理')
    states = [v for v in service.get('volumes', []) if isinstance(v, dict) and v.get('target') == '/app/state']
    if len(states) != 1 or states[0].get('type') != 'volume':
        raise UpdateError('UNSUPPORTED_DEPLOYMENT', '状态卷必须独立挂载')
    state = spec.get('volumes', {}).get(states[0].get('source'), {})
    if states[0].get('source') == assets[0].get('source') or state.get('name') == value.get('name'):
        raise UpdateError('UNSUPPORTED_DEPLOYMENT', '状态与附件不能共享同一底层数据卷')
    return '/app/state', 'CODEPIER_HUB_STATE_VOLUME'


def data_volume(spec):
    service = spec.get('services', {}).get('hub', {})
    mount_path, _ = storage_layout(spec)
    mounts = [v for v in service.get('volumes', []) if isinstance(v, dict) and v.get('target') == mount_path]
    if len(mounts) != 1 or mounts[0].get('type') != 'volume':
        raise UpdateError('UNSUPPORTED_DEPLOYMENT', '一键更新需要 Hub 使用独立命名数据卷；自定义挂载请手动维护')
    key = mounts[0]['source']
    volume = spec.get('volumes', {}).get(key, {})
    name = volume.get('name', '')
    if not volume.get('external') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
        raise UpdateError('UNSUPPORTED_DEPLOYMENT', 'Hub 数据卷必须是明确的外部命名卷')
    return key, name


def compose_command(spec_path, project):
    return ['docker', 'compose', '--project-name', project, '-f', str(spec_path)]


def updated_env(content, changes):
    text = content.decode('utf-8')
    lines = text.splitlines(keepends=True)
    result = []
    found = set()
    for line in lines:
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        if match and match[1] in changes:
            if match[1] not in found:
                result.append(f'{match[1]}={changes[match[1]]}\n')
                found.add(match[1])
        else:
            result.append(line)
    if result and not result[-1].endswith('\n'):
        result[-1] += '\n'
    result.extend(f'{key}={value}\n' for key, value in changes.items() if key not in found)
    return ''.join(result).encode('utf-8')


# This copy program belongs to the installed updater, not to downloaded code.
COPY_DATA = r'''
import hashlib,json,os,shutil,sqlite3,stat
from pathlib import Path
src,dst=Path('/backup'),Path('/app/data')
if not (src/'master.key').is_file() or not (src/'hub.sqlite3').is_file():
    raise RuntimeError('Original Hub identity missing')
if any(dst.iterdir()):
    raise RuntimeError('Candidate data volume is not empty')
files=total=0
for root,dirs,names in os.walk(src,followlinks=False):
    base=Path(root)
    target=dst/base.relative_to(src)
    target.mkdir(parents=True,exist_ok=True)
    for name in dirs:
        if (base/name).is_symlink(): raise RuntimeError('Data symlink')
    for name in names:
        original=base/name
        if name=='.hub.lock' and base==src: continue
        info=original.lstat()
        if not stat.S_ISREG(info.st_mode): raise RuntimeError('Special data file')
        copied=target/name
        shutil.copy2(original,copied)
        def digest(p):
            h=hashlib.sha256()
            with p.open('rb') as f:
                for chunk in iter(lambda:f.read(65536),b''): h.update(chunk)
            return h.digest()
        if digest(original)!=digest(copied): raise RuntimeError('Data copy mismatch')
        with copied.open('rb') as f: os.fsync(f.fileno())
        files+=1; total+=info.st_size
for root,dirs,names in os.walk(dst,topdown=False):
    directory=Path(root)
    os.chmod(directory,stat.S_IMODE((src/directory.relative_to(dst)).stat().st_mode))
    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
with sqlite3.connect('file:/app/data/hub.sqlite3?mode=ro',uri=True) as db:
    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise RuntimeError('SQLite integrity')
print(json.dumps({'files':files,'bytes':total,'integrity':'ok'}))
'''

COPY_STATE = r'''

import hashlib,json,os,shutil,sqlite3,stat
from pathlib import Path
src,dst=Path('/backup'),Path('/app/data')
allowed={'hub.sqlite3','hub.sqlite3-wal','hub.sqlite3-shm','master.key','master.keys.json','.codepier-state.json','.hub.lock','.keys.lock','native-cli','rekey.json','.rekey'}
if any(dst.iterdir()): raise RuntimeError('Candidate state is not empty')
if any(p.name not in allowed or p.is_symlink() for p in src.iterdir()): raise RuntimeError('Unknown state files; inspect before snapshot')
if (src/'rekey.json').exists() or (src/'.rekey').exists(): raise RuntimeError('Key rotation recovery material requires explicit archive review before split-state update')
marker=json.loads((src/'.codepier-state.json').read_text())
if marker.get('schema_version')!=1 or marker.get('layout')!='split-state-v1': raise RuntimeError('State receipt invalid')
required=sum((src/n).stat().st_size for n in ('hub.sqlite3','hub.sqlite3-wal','native-cli/native.sqlite3','native-cli/native.sqlite3-wal') if (src/n).is_file())+512*1024*1024
if shutil.disk_usage(dst).free < required: raise RuntimeError('Insufficient target state capacity')
files=total=0
for name in ('master.key','master.keys.json'):
    original=src/name
    if not original.exists():
        if name=='master.keys.json': continue
        raise RuntimeError('State identity missing')
    if not stat.S_ISREG(original.lstat().st_mode): raise RuntimeError('Invalid state identity')
    copied=dst/name
    shutil.copy2(original,copied)
    os.chmod(copied,0o600)
    if hashlib.sha256(original.read_bytes()).digest()!=hashlib.sha256(copied.read_bytes()).digest(): raise RuntimeError('Identity mismatch')
    with copied.open('rb') as handle: os.fsync(handle.fileno())
    files+=1;total+=copied.stat().st_size
for name in ('hub.sqlite3','native-cli/native.sqlite3'):
    original=src/name
    if name.startswith('native-cli'):
        folder=src/'native-cli'
        if not folder.exists(): continue
        if folder.is_symlink() or any(p.name not in {'native.sqlite3','native.sqlite3-wal','native.sqlite3-shm','locks'} or p.is_symlink() for p in folder.iterdir()): raise RuntimeError('Unknown native state')
    if original.is_symlink() or not original.is_file(): raise RuntimeError('Database missing')
    copied=dst/name;copied.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    # The old Hub is stopped. Copy its WAL as part of the same quiesced state.
    # Opening a WAL database directly on a read-only mount can require creating
    # shared-memory files. Never use immutable=1 to bypass that: it ignores WAL.
    if original.with_name(original.name+'-journal').exists(): raise RuntimeError('Unexpected rollback journal')
    def digest(path):
        h=hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda:handle.read(65536),b''): h.update(chunk)
        return h.digest()
    for suffix in ('','-wal'):
        source_file=original.with_name(original.name+suffix)
        if not source_file.exists(): continue
        if source_file.is_symlink() or not stat.S_ISREG(source_file.lstat().st_mode): raise RuntimeError('Unsafe database file')
        target_file=copied.with_name(copied.name+suffix)
        shutil.copy2(source_file,target_file);os.chmod(target_file,0o600)
        if digest(source_file)!=digest(target_file): raise RuntimeError('Database copy mismatch')
        with target_file.open('rb') as handle: os.fsync(handle.fileno())
    target=sqlite3.connect(copied)
    try:
        if target.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]!=0: raise RuntimeError('Candidate WAL busy')
        if target.execute('PRAGMA journal_mode=DELETE').fetchone()[0]!='delete': raise RuntimeError('Candidate journal mode')
        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise RuntimeError('SQLite integrity')
    finally:
        target.close()
    os.chmod(copied,0o600)
    with copied.open('rb') as handle: os.fsync(handle.fileno())
    files+=1;total+=copied.stat().st_size
for root,dirs,names in os.walk(dst,topdown=False):
    os.chmod(root,0o700)
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
# Receipt is published only after all databases and identities are durable.
receipt=dst/'.codepier-state.json'
with receipt.open('x',encoding='utf-8') as handle:
    json.dump(marker,handle,sort_keys=True);handle.flush();os.fsync(handle.fileno())
os.chmod(receipt,0o600)
fd=os.open(dst,os.O_RDONLY|os.O_DIRECTORY)
try: os.fsync(fd)
finally: os.close(fd)
print(json.dumps({'files':files,'bytes':total,'integrity':'ok','state_layout':'split-state-v1','attachments_copied':False}))
'''

# First transition retains all non-state files in the original stable asset volume.
COPY_MIGRATION = COPY_STATE.replace(
    "if any(p.name not in allowed or p.is_symlink() for p in src.iterdir()): raise RuntimeError('Unknown state files; inspect before snapshot')",
    "if any((src/n).is_symlink() for n in allowed): raise RuntimeError('Symlink state identity')").replace(
    "marker=json.loads((src/'.codepier-state.json').read_text())",
    "marker={'schema_version':1,'layout':'split-state-v1'}")

PREFLIGHT = """import json
from pathlib import Path
from shared.util import VERSION
from hub.agent_install import AgentPackage
import hub.panel_update, shared.panel_maintenance
try:
    from hub.state_layout import STATE_LAYOUT
except ImportError:
    STATE_LAYOUT = 'legacy'
p=AgentPackage(Path('/app')).build()
print(json.dumps({'version':VERSION,'agent_sha256':p.sha256,'agent_bytes':len(p.content),'state_layout':STATE_LAYOUT}))
"""
HEALTH = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3).read().decode())"
MANIFEST = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/agent/manifest.json',timeout=5).read().decode())"


class DockerRuntime:
    def __init__(self, root, home, config, execute=run):
        self.root, self.home, self.config, self.execute = Path(root), Path(home), config, execute
        self.run_dir = self.home / 'run'
        self.gate = self.run_dir / 'maintenance.json'

    def command(self, command, *, timeout=120, job=None):
        log = self.home / 'jobs' / job['id'] / 'commands.log' if job else None
        return self.execute(command, cwd=self.root, timeout=timeout, log=log)

    def compose(self, job, which, *args, timeout=120):
        path = self.home / 'jobs' / job['id'] / f'{which}.json'
        return self.command(compose_command(path, self.config['project']) + list(args), timeout=timeout, job=job)

    def set_gate(self, job):
        atomic_json(self.gate, {'operation_id': job['id'], 'since': time.time()}, mode=0o644)

    def clear_gate(self):
        self.gate.unlink(missing_ok=True)
        sync_directory(self.run_dir)

    def prepare(self, job, source):
        current = read_json(self.home / 'current.json')
        for name, sha in current['files'].items():
            if fingerprint(self.root / name) != sha:
                raise UpdateError('DEPLOYMENT_CHANGED', '宿主机部署配置已改变；请重新安装更新服务后再操作', 409)
        spec = copy.deepcopy(current['compose'])
        key, old_volume = data_volume(spec)
        path = self.home / 'jobs' / job['id']
        save_compose(path / 'old.json', spec)
        atomic_bytes(path / 'env.before', (self.root / '.env').read_bytes())
        ids = self.compose(job, 'old', 'ps', '-q', 'hub').split()
        if len(ids) != 1:
            raise UpdateError('UNSUPPORTED_DEPLOYMENT', '只支持单个健康运行的 Hub 容器', 409)
        container = json.loads(self.command(['docker', 'inspect', ids[0]], job=job))[0]
        labels = container.get('Config', {}).get('Labels', {})
        mounts = container.get('Mounts', [])
        mount_path, volume_env = storage_layout(spec)
        mounted = [m for m in mounts if m.get('Destination') == mount_path]
        if mount_path == '/app/state':
            asset_key = next(v['source'] for v in spec['services']['hub']['volumes'] if v.get('target') == '/app/data')
            asset_name = spec['volumes'][asset_key]['name']
            live_assets = [m for m in mounts if m.get('Destination') == '/app/data']
            if len(live_assets) != 1 or live_assets[0].get('Name') != asset_name:
                raise UpdateError('DEPLOYMENT_CHANGED', '运行中的附件卷与独立存储配置不一致', 409)
        socket_mount = [m for m in mounts if m.get('Destination') == '/run/codepier-updater']
        if (labels.get('com.docker.compose.project') != self.config['project'] or
                labels.get('com.docker.compose.service') != 'hub' or
                len(mounted) != 1 or mounted[0].get('Name') != old_volume or
                len(socket_mount) != 1 or socket_mount[0].get('RW') or
                Path(socket_mount[0].get('Source', '')).resolve() != self.run_dir.resolve()):
            raise UpdateError('DEPLOYMENT_CHANGED', '运行中的 Hub 与受管配置、数据卷或更新服务挂载不一致', 409)
        spec['services']['hub'].pop('build', None)
        spec['services']['hub']['image'] = container['Image']
        save_compose(path / 'old.json', spec)
        observed = json.loads(self.command(['docker', 'exec', ids[0], 'python', '-c', HEALTH], job=job))
        if observed.get('status') != 'ok' or observed.get('version') != job['from_version']:
            raise UpdateError('VERSION_CHANGED', '运行中的 Hub 版本已变化，请重新检查', 409)
        new_volume = 'codepier-update-data-' + job['id']
        image = 'codepier-update:' + job['id']
        target = copy.deepcopy(spec)
        target['services']['hub']['image'] = image
        migration = job.get('kind') == 'migrate-state' or (job.get('kind') == 'apply' and mount_path == '/app/data')
        if migration:
            live_env = dict(v.split('=', 1) for v in container.get('Config', {}).get('Env', []) if '=' in v)
            if (live_env.get('HUB_STATE_DIR') or live_env.get('HUB_DATA_DIR', '/app/data') != '/app/data' or
                    any(v.get('Destination') == '/app/state' for v in mounts) or
                    any(v.get('target') == '/app/state' for v in target['services']['hub']['volumes'])):
                raise UpdateError('LAYOUT_SOURCE_MISMATCH', '实际数据库路径或已有状态挂载不适合首次迁移', 409)
            if mount_path != '/app/data' or 'hub-state' in target['volumes']:
                raise UpdateError('LAYOUT_ALREADY_SET', '布局已分离或状态卷名称冲突；未改变部署', 409)
            if job.get('kind') == 'migrate-state':
                image = container['Image']
                target['services']['hub']['image'] = image
            target['services']['hub']['environment']['HUB_STATE_DIR'] = '/app/state'
            target['services']['hub']['environment']['HUB_DATA_DIR'] = '/app/data'
            target['services']['hub']['volumes'].append({'type': 'volume', 'source': 'hub-state', 'target': '/app/state'})
            target['volumes']['hub-state'] = {'external': True, 'name': new_volume}
            volume_env = 'CODEPIER_HUB_STATE_VOLUME'
        else:
            target['volumes'][key]['name'] = new_volume
        save_compose(path / 'target.json', target)
        return {'old_volume': old_volume, 'new_volume': new_volume, 'old_image': container['Image'],
                'image': image, 'source': str(source.relative_to(self.root)), 'old_container': ids[0],
                'state_layout': 'split-state-v1' if migration or mount_path == '/app/state' else 'legacy',
                'volume_env': volume_env, 'layout_migration': migration,
                'asset_volume': old_volume if migration else asset_name if mount_path == '/app/state' else '',
                'layout_config': '.codepier-updater/layouts/' + job['id'] + '.json' if migration or mount_path == '/app/state' else ''}

    def build(self, job, source):
        if job.get('kind') != 'migrate-state':
            self.command(['docker', 'build', '--pull', '--tag', job['image'], '--file', str(source / 'Dockerfile'), str(source)], timeout=1800, job=job)
        metadata = json.loads(self.command(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                             '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                             '--entrypoint', 'python', job['image'], '-c', PREFLIGHT], job=job))
        if (metadata.get('version') != job['target_version'] or
                not re.fullmatch(r'[a-f0-9]{64}', metadata.get('agent_sha256', '')) or
                not 0 < metadata.get('agent_bytes', 0) <= 8 * 1024 * 1024):
            raise UpdateError('PREFLIGHT_FAILED', '候选镜像或配套 Agent 文件校验失败', 500)
        if job.get('state_layout') == 'split-state-v1' and metadata.get('state_layout') != 'split-state-v1':
            raise UpdateError('STATE_LAYOUT_UNSUPPORTED', '候选版本不支持独立状态卷；未停止旧服务', 409)
        image_id = self.command(['docker', 'image', 'inspect', job['image'], '--format', '{{.Id}}'], job=job).strip()
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
            raise UpdateError('PREFLIGHT_FAILED', '候选镜像固定身份无法核实', 500)
        metadata['image_id'] = image_id
        return metadata

    def validate_files(self):
        current = read_json(self.home / 'current.json')
        for name, expected in current['files'].items():
            if fingerprint(self.root / name) != expected:
                raise UpdateError('DEPLOYMENT_CHANGED', '部署配置在更新期间被其他操作修改；拒绝覆盖，请在宿主机核查', 409)

    def quiesce(self, job):
        self.validate_files()
        self.set_gate(job)
        # The gate blocks new HTTP mutations, tool dispatches and WebSockets.
        # Existing admitted HTTP requests must drain as well as durable operations.
        end = time.monotonic() + 30
        while time.monotonic() < end:
            health = json.loads(self.command(['docker', 'exec', job['old_container'], 'python', '-c', HEALTH], timeout=10, job=job))
            if health.get('panel_update', {}).get('ready') is True:
                return
            time.sleep(1)
        raise UpdateError('HUB_BUSY', '仍有执行中或排队操作；没有中断任务，请空闲后重新更新', 409)

    def stop(self, job):
        self.compose(job, 'old', 'stop', '--timeout', '30', 'hub', timeout=60)
        info = json.loads(self.command(['docker', 'inspect', job['old_container']], job=job))[0]
        if info.get('State', {}).get('Running'):
            raise UpdateError('STOP_FAILED', '旧 Hub 未停止，拒绝复制或切换数据', 500)

    def copy_data(self, job):
        existing = [json.loads(line) for line in self.command(['docker', 'volume', 'ls', '--format', '{{json .Name}}'], job=job).splitlines() if line]
        if job['new_volume'] in existing:
            raise UpdateError('VOLUME_EXISTS', '候选数据卷已存在；拒绝覆盖或混合数据', 409)
        self.command(['docker', 'volume', 'create', '--label', 'com.codepier.update=' + job['id'], job['new_volume']], job=job)
        raw = self.command(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--user', '10001:10001',
                            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                            '--volume', job['old_volume'] + ':/backup:ro',
                            '--volume', job['new_volume'] + ':/app/data',
                            '--entrypoint', 'python', job['old_image'], '-c', COPY_MIGRATION if job.get('layout_migration') else COPY_STATE if job.get('state_layout') == 'split-state-v1' else COPY_DATA], timeout=1800, job=job)
        return json.loads(raw)

    def start(self, job, which):
        self.compose(job, which, 'up', '-d', '--no-deps', '--no-build', '--force-recreate',
                     '--pull', 'never', '--wait', '--wait-timeout', '90', 'hub', timeout=120)

    def verify(self, job, which='target'):
        ids = self.compose(job, which, 'ps', '-q', 'hub').split()
        if len(ids) != 1:
            raise UpdateError('HEALTH_FAILED', '更新后的 Hub 容器数量异常', 500)
        health = json.loads(self.command(['docker', 'exec', ids[0], 'python', '-c', HEALTH], job=job))
        expected = job['target_version'] if which == 'target' else job['from_version']
        if health.get('status') != 'ok' or health.get('version') != expected or health.get('panel_update', {}).get('maintenance') is not True:
            raise UpdateError('HEALTH_FAILED', 'Hub 版本、健康状态或维护保护校验失败', 500)
        if which == 'target':
            agent = json.loads(self.command(['docker', 'exec', ids[0], 'python', '-c', MANIFEST], job=job))
            if (agent.get('version') != expected or agent.get('sha256') != job['package']['agent_sha256'] or
                    agent.get('bytes') != job['package']['agent_bytes']):
                raise UpdateError('AGENT_PACKAGE_MISMATCH', '新 Hub 提供的 Agent 安装包与预检不一致', 500)
        if job.get('layout_migration'):
            actual = json.loads(self.command(['docker', 'inspect', ids[0]], job=job))[0]
            selected = read_json(self.home / 'jobs' / job['id'] / (which + '.spec.json'))
            expected_mounts = {v['target']: selected['volumes'][v['source']]['name']
                for v in selected['services']['hub']['volumes'] if v.get('type') == 'volume'}
            actual_mounts = {v.get('Destination'): v.get('Name') for v in actual.get('Mounts', [])}
            if any(actual_mounts.get(path) != name for path, name in expected_mounts.items()):
                raise UpdateError('LAYOUT_MISMATCH', '实际状态或附件卷与迁移目标不一致', 500)
            expected_state = '/app/state' if which == 'target' else None
            actual_env = dict(v.split('=', 1) for v in actual.get('Config', {}).get('Env', []) if '=' in v)
            if ((actual_env.get('HUB_STATE_DIR') or None) != expected_state or
                    actual_env.get('HUB_DATA_DIR', '/app/data') != '/app/data' or
                    (which == 'old' and '/app/state' in actual_mounts) or
                    actual.get('Image') != (job['package'].get('image_id') if which == 'target' and job.get('kind') == 'apply' else job['old_image'])):
                raise UpdateError('LAYOUT_MISMATCH', '实际镜像或状态路径与已核验迁移目标不一致', 500)
        return health

    def committed_env(self, job, before):
        changes = {'CODEPIER_HUB_IMAGE': job['image'],
                   job.get('volume_env', 'CODEPIER_HUB_DATA_VOLUME'): job['new_volume']}
        if job.get('state_layout') == 'split-state-v1':
            changes['COMPOSE_FILE'] = job['layout_config']
        if job.get('kind') != 'migrate-state':
            changes['CODEPIER_HUB_SOURCE'] = job['source']
        return updated_env(before, changes)

    def commit(self, job):
        # Caller durably records COMMITTED after this returns, before removing gate.
        self.validate_files()
        current = read_json(self.home / 'current.json')
        if fingerprint(self.root / '.env') != current['files']['.env']:
            raise UpdateError('DEPLOYMENT_CHANGED', '更新期间 .env 被其他操作修改，拒绝覆盖', 409)
        before = (self.home / 'jobs' / job['id'] / 'env.before').read_bytes()
        content = self.committed_env(job, before)
        if job.get('state_layout') == 'split-state-v1':
            selected = read_json(self.home / 'jobs' / job['id'] / 'target.spec.json')
            layout = self.root / job['layout_config']
            if layout.exists() or layout.is_symlink():
                raise UpdateError('LAYOUT_CONFIG_EXISTS', '迁移配置已存在；拒绝覆盖，需核对原操作', 409)
            save_compose(layout, selected)
            current['files'] = {job['layout_config']: fingerprint(layout)}
        atomic_bytes(self.root / '.env', content)
        current['compose'] = read_json(self.home / 'jobs' / job['id'] / 'target.spec.json')
        current['version'] = job['target_version']
        current['files']['.env'] = hashlib.sha256(content).hexdigest()
        atomic_json(self.home / 'current.json', current)

    def rollback(self, job):
        # Target has NEVER accepted traffic: the host-owned gate stays closed.
        self.set_gate(job)
        self.compose(job, 'old', 'stop', '--timeout', '30', 'hub', timeout=60)
        self.start(job, 'old')
        self.verify(job, 'old')
        before = self.home / 'jobs' / job['id'] / 'env.before'
        current = read_json(self.home / 'jobs' / job['id'] / 'current.before.json')
        actual = fingerprint(self.root / '.env')
        old_sha = hashlib.sha256(before.read_bytes()).hexdigest()
        committed_env = hashlib.sha256(self.committed_env(job, before.read_bytes())).hexdigest()
        if actual not in {old_sha, committed_env}:
            raise UpdateError('RECOVERY_REQUIRED', '运行版本已恢复，但宿主机配置被另行修改；请人工核对', 500)
        atomic_bytes(self.root / '.env', before.read_bytes())
        atomic_json(self.home / 'current.json', current)
        self.clear_gate()
