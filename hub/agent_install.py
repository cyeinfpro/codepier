"""Public, pinned Agent source package and single-use enrollment tickets.

Only the authenticated panel can mint tickets. The permanent device key travels
in a one-time POST response, never in an installer URL, command, or package.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shlex
import stat
import threading
import time
from typing import Literal
import zipfile

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.crypto import digest, token
from hub import iam
from shared.util import DevError, VERSION, normalize_url, valid_json_value

TICKET_SECONDS = 15 * 60
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
PUBLIC_SCRIPTS = {'posix': 'deploy/install-from-hub.sh', 'windows': 'deploy/install-from-hub.ps1'}
DEPLOY_FILES = (
    'deploy/install-from-hub.sh', 'deploy/install-from-hub.ps1',
    'deploy/install-agent.sh', 'deploy/install-agent.ps1',
    'deploy/start-agent.sh', 'deploy/start-agent.cmd', 'deploy/agent.service',
)
SCHEMA = '''CREATE TABLE IF NOT EXISTS agent_install_tickets (
    token_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL UNIQUE,
    secret_fingerprint TEXT NOT NULL,
    hub_url TEXT NOT NULL,
    expires REAL NOT NULL,
    created REAL NOT NULL
)'''


class DeviceLifecycleInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')
    confirmation: str = Field(default='', max_length=80)

    @model_validator(mode='before')
    @classmethod
    def json_safe(cls, value):
        if not valid_json_value(value):
            raise ValueError('生命周期参数必须是有效的 UTF-8 JSON')
        return value


class InstallTicketInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    hub_url: str = Field(min_length=1, max_length=500)
    platform: Literal['posix', 'windows']
    allow_root: str = Field(min_length=1, max_length=2048)
    enable_execution: bool = True

    @model_validator(mode='before')
    @classmethod
    def json_safe(cls, value):
        if not valid_json_value(value):
            raise ValueError('安装参数必须是有效的 UTF-8 JSON')
        return value


class AgentCommandInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    hub_url: str = Field(min_length=1, max_length=500)
    platform: Literal['posix', 'windows']
    install_dir: str = Field(default='', max_length=2048)


def validate_root(value, platform):
    if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise DevError('INVALID_ALLOW_ROOT', '请填写这台设备要授权的绝对目录路径')
    path = PureWindowsPath(value) if platform == 'windows' else PurePosixPath(value)
    if not path.is_absolute() or platform == 'windows' and value.startswith(('\\\\.\\', '\\\\?\\')):
        raise DevError('INVALID_ALLOW_ROOT', '目录必须是所选系统的绝对路径，不能使用环境变量或相对目录')
    return value


def powershell_quote(value):
    return "'" + value.replace("'", "''") + "'"


def install_command(platform, hub_url, enrollment_token, package_sha256, allow_root, bootstrap_sha256=None, *, action='install', expected_device='', install_dir='', enable_execution=True):
    if type(enable_execution) is not bool:
        raise ValueError('Execution option must be a boolean')
    if action not in {'install','upgrade','uninstall'}:
        raise ValueError('Unsupported installer action')
    if action != 'install' and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', expected_device):
        raise ValueError('A valid target device is required for maintenance commands')
    if bootstrap_sha256 is not None and not re.fullmatch(r'[a-f0-9]{64}', bootstrap_sha256):
        raise DevError('INSTALL_PACKAGE_CHANGED', '安装脚本校验值无效，请在面板重新生成安装命令', 409)
    if platform == 'posix':
        values = ('--hub', hub_url, '--token', enrollment_token, '--sha256', package_sha256, '--allow', allow_root) if action == 'install' else (
            '--hub', hub_url, '--sha256', package_sha256, '--'+action, '--expected-device', expected_device)
        if action == 'install':
            values += ('--shell', 'full' if enable_execution else 'disabled')
        if install_dir:
            values += ('--install-dir', install_dir)
        args = ' '.join(shlex.quote(value) for value in values)
        bootstrap_check = ''
        if bootstrap_sha256:
            bootstrap_check = (
                ' && if command -v sha256sum >/dev/null 2>&1; then '
                'codepier_bootstrap_sha=$(sha256sum "$codepier_install_tmp/install.sh" | awk \'{print $1}\'); '
                'elif command -v shasum >/dev/null 2>&1; then '
                'codepier_bootstrap_sha=$(shasum -a 256 "$codepier_install_tmp/install.sh" | awk \'{print $1}\'); '
                'else echo "安装脚本校验工具不可用" >&2; exit 1; fi && '
                '[[ "$codepier_bootstrap_sha" == '+shlex.quote(bootstrap_sha256)+' ]] || '
                '{ echo "安装脚本校验失败，请在面板重新生成安装命令" >&2; exit 1; }')
        return ('(umask 077; codepier_install_tmp=$(mktemp -d) || exit 1; '
                'trap \'rm -rf "$codepier_install_tmp"\' EXIT; '
                'curl --fail --silent --show-error --location --max-time 60 '
                '--output "$codepier_install_tmp/install.sh" ' + shlex.quote(hub_url+'/agent/install.sh') +
                bootstrap_check + ' && bash "$codepier_install_tmp/install.sh" ' + args + ')')
    arguments = ' '.join(powershell_quote(value) for value in (hub_url, enrollment_token, package_sha256, allow_root))
    hub, ticket, sha, root = (powershell_quote(value) for value in (hub_url, enrollment_token, package_sha256, allow_root))
    maintenance = '' if action == 'install' else ' -Action '+action+' -ExpectedDevice '+powershell_quote(expected_device)
    if action == 'install':
        maintenance += ' -Shell '+powershell_quote('full' if enable_execution else 'disabled')
    if install_dir:
        maintenance += ' -InstallDir '+powershell_quote(install_dir)
    bootstrap_check = ''
    if bootstrap_sha256:
        bootstrap_check = (" if ((Get-FileHash -LiteralPath $codepierInstallScript -Algorithm SHA256).Hash.ToLowerInvariant() -cne "
                           +powershell_quote(bootstrap_sha256)+") { throw '安装脚本校验失败，请在面板重新生成安装命令' }; ")
    return ('& { $ErrorActionPreference = \'Stop\'; '
            '$codepierInstallTemp = Join-Path ([IO.Path]::GetTempPath()) (\'codepier-\' + [guid]::NewGuid().ToString(\'N\')); '
            'New-Item -ItemType Directory -Path $codepierInstallTemp | Out-Null; '
            'try { $codepierInstallScript = Join-Path $codepierInstallTemp \'install.ps1\'; '
            'Invoke-WebRequest -UseBasicParsing -Uri ' + powershell_quote(hub_url+'/agent/install.ps1') +
            ' -OutFile $codepierInstallScript; '+bootstrap_check+
            '& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $codepierInstallScript '
            '-Hub '+hub+' -Token '+ticket+' -Sha256 '+sha+' -AllowRoot '+root+maintenance+'; '
            'if ($LASTEXITCODE -ne 0) { throw \'Agent 安装失败，请查看上方错误\' } '
            '} finally { Remove-Item -LiteralPath $codepierInstallTemp -Recurse -Force } }')


class AgentPackage:
    """Capture one deterministic, bounded artifact for this running Hub."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.Lock()
        self.content = None
        self.scripts = {}
        self.sha256 = None

    def _read(self, relative):
        path = self.root / relative
        current = self.root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('Package symlinks are not allowed')
        if not path.is_file() or path.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError('Package source unavailable or too large')
        with path.open('rb') as stream:
            content = stream.read(MAX_PACKAGE_BYTES+1)
        if len(content) > MAX_PACKAGE_BYTES:
            raise ValueError('Package source too large')
        return content

    def build(self):
        with self.lock:
            if self.content is not None:
                return self
            try:
                names = ['requirements-agent.txt', 'scripts/install_agent.py', 'scripts/agent_lifecycle.py']
                for directory in ('agent', 'shared'):
                    folder = self.root / directory
                    if folder.is_symlink() or not folder.is_dir():
                        raise ValueError('Package source directory unavailable')
                    names.extend(f'{directory}/{entry.name}' for entry in folder.iterdir()
                                 if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*\.py', entry.name))
                names.extend(name for name in DEPLOY_FILES if (self.root/name).exists())
                if not {'agent/__main__.py', 'agent/lifecycle.py', 'shared/util.py', 'shared/agent_lifecycle.py',
                        'scripts/install_agent.py', 'scripts/agent_lifecycle.py', *PUBLIC_SCRIPTS.values()}.issubset(names):
                    raise ValueError('Required package source missing')
                output = io.BytesIO()
                total = 0
                scripts = {}
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    for name in sorted(set(names)):
                        content = self._read(name)
                        total += len(content)
                        if total > MAX_PACKAGE_BYTES:
                            raise ValueError('Package source exceeds size limit')
                        info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.create_system = 3
                        info.external_attr = (stat.S_IFREG | (0o755 if name.endswith('.sh') else 0o644)) << 16
                        archive.writestr(info, content)
                        if name in PUBLIC_SCRIPTS.values():
                            if len(content) > 256 * 1024:
                                raise ValueError('Bootstrap script exceeds size limit')
                            scripts[name] = content
                content = output.getvalue()
                if len(content) > MAX_PACKAGE_BYTES:
                    raise ValueError('Compressed package exceeds size limit')
                self.content, self.scripts = content, scripts
                self.sha256 = hashlib.sha256(content).hexdigest()
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                raise DevError('INSTALL_PACKAGE_UNAVAILABLE', 'Agent 安装包尚未就绪，请稍后重试或下载配对文件手动安装', 503) from exc
            return self

    def metadata(self, hub_url):
        self.build()
        return {'url': hub_url+'/agent/agent.zip?sha256='+self.sha256,
                'sha256': self.sha256, 'bytes': len(self.content), 'version': VERSION}


def make_agent_install_router(runtime, auth, source_root=None):
    store = runtime.store
    store.execute(SCHEMA)
    package = AgentPackage(source_root or Path(__file__).resolve().parent.parent)
    router = APIRouter()

    @router.get('/agent/install.sh')
    async def posix_script():
        bundle = package.build()
        return Response(bundle.scripts[PUBLIC_SCRIPTS['posix']], media_type='text/plain',
                        headers={'Cache-Control': 'no-store'})

    @router.get('/agent/install.ps1')
    async def windows_script():
        bundle = package.build()
        return Response(bundle.scripts[PUBLIC_SCRIPTS['windows']], media_type='text/plain',
                        headers={'Cache-Control': 'no-store'})

    @router.get('/agent/manifest.json')
    async def agent_manifest():
        # Public source metadata only; no enrollment, identity, credential or local path.
        return JSONResponse(package.metadata(''), headers={'Cache-Control': 'no-store'})

    @router.post('/api/devices/{device_id}/agent-commands')
    async def agent_commands(device_id: str, request: Request, body: AgentCommandInput):
        principal=auth.panel(request, True)
        iam.require_device(store,principal,device_id,manage=True)
        if not store.one('SELECT id FROM devices WHERE id=?', (device_id,)):
            raise DevError('NOT_FOUND', '设备不存在', 404)
        try:
            hub_url = normalize_url(body.hub_url)
        except ValueError as exc:
            raise DevError('INVALID_URL', str(exc)) from exc
        install_dir = validate_root(body.install_dir, body.platform) if body.install_dir else ''
        bundle = package.build()
        script_sha = hashlib.sha256(bundle.scripts[PUBLIC_SCRIPTS[body.platform]]).hexdigest()
        commands = {action: install_command(body.platform, hub_url, '', bundle.sha256, '', script_sha,
                                            action=action, expected_device=device_id, install_dir=install_dir)
                    for action in ('upgrade', 'uninstall')}
        return JSONResponse({'commands': commands, 'platform': body.platform, 'package': package.metadata(hub_url)},
                            headers={'Cache-Control': 'no-store'})

    @router.get('/agent/agent.zip')
    async def agent_package(sha256: str = Query(pattern=r'^[a-f0-9]{64}$')):
        bundle = package.build()
        if not hmac.compare_digest(sha256, bundle.sha256):
            raise DevError('INSTALL_PACKAGE_CHANGED', '此命令对应的 Agent 安装包已变化，请在面板重新生成安装命令', 409)
        return Response(bundle.content, media_type='application/zip', headers={
            'Cache-Control': 'public, max-age=31536000, immutable',
            'Content-Disposition': f'attachment; filename="codepier-agent-{VERSION}.zip"',
            'ETag': '"'+bundle.sha256+'"', 'X-Content-SHA256': bundle.sha256,
        })

    @router.post('/api/devices/{device_id}/install-ticket')
    async def install_ticket(device_id: str, request: Request, body: InstallTicketInput):
        principal = auth.panel(request, True)
        iam.require_device(store,principal,device_id,manage=True)
        try:
            hub_url = normalize_url(body.hub_url)
        except ValueError as exc:
            raise DevError('INVALID_URL', str(exc)) from exc
        allow_root = validate_root(body.allow_root, body.platform)
        now = time.time()
        with store.lock, store.db:
            device = store.db.execute('SELECT d.name,d.secret,d.enabled,s.active AS space_active,COALESCE(u.active,1) AS owner_active FROM devices d JOIN spaces s ON s.id=d.space_id LEFT JOIN iam_users u ON u.user_id=d.owner_user_id WHERE d.id=?', (device_id,)).fetchone()
            if not device:
                raise DevError('NOT_FOUND', '设备不存在', 404)
            if not device['enabled']:
                raise DevError('DEVICE_DISABLED', '设备已停用，启用后才能生成安装命令', 409)
            metadata = package.metadata(hub_url)
            enrollment_token = 'rdi_'+token(32)
            bootstrap_sha256 = hashlib.sha256(package.scripts[PUBLIC_SCRIPTS[body.platform]]).hexdigest()
            command = install_command(body.platform, hub_url, enrollment_token, metadata['sha256'], allow_root,
                                      bootstrap_sha256=bootstrap_sha256, enable_execution=body.enable_execution)
            expires = now + TICKET_SECONDS
            fingerprint = digest(store.decrypt(device['secret']))
            store.db.execute('DELETE FROM agent_install_tickets WHERE expires<=? OR device_id=?', (now, device_id))
            store.db.execute('INSERT INTO agent_install_tickets VALUES (?,?,?,?,?,?)',
                (digest(enrollment_token), device_id, fingerprint, hub_url, expires, now))
            iam.audit(store,principal,'device.install_ticket',device_id,detail={'platform':body.platform,'expires_at':expires,'package_sha256':metadata['sha256'],'fresh_install_execution':body.enable_execution})
        return JSONResponse({'command': command, 'platform': body.platform, 'expires_at': expires, 'package': metadata},
                            headers={'Cache-Control': 'no-store'})

    async def lifecycle_action(device_id: str, request: Request, body: DeviceLifecycleInput, action: str):
        principal = auth.panel(request, True)
        iam.require_device(store,principal,device_id,manage=True)
        device = store.one('SELECT name FROM devices WHERE id=?', (device_id,))
        if not device:
            raise DevError('NOT_FOUND', '设备不存在', 404)
        if action == 'agent_uninstall' and body.confirmation != device['name']:
            raise DevError('CONFIRMATION_REQUIRED', '请输入完整设备名称以确认卸载', 409)
        args = {'idempotency_key': body.idempotency_key}
        if action == 'agent_update':
            bundle = package.build()
            args.update({'package_sha256': bundle.sha256, 'package_bytes': len(bundle.content),
                         'target_version': VERSION})
        return await runtime.dispatch_device_action(action, args, device_id, principal)

    @router.post('/api/devices/{device_id}/agent-update')
    async def update_agent(device_id: str, request: Request, body: DeviceLifecycleInput):
        return await lifecycle_action(device_id, request, body, 'agent_update')

    @router.post('/api/devices/{device_id}/agent-restart')
    async def restart_agent(device_id: str, request: Request, body: DeviceLifecycleInput):
        return await lifecycle_action(device_id, request, body, 'agent_restart')

    @router.post('/api/devices/{device_id}/agent-uninstall')
    async def uninstall_agent(device_id: str, request: Request, body: DeviceLifecycleInput):
        return await lifecycle_action(device_id, request, body, 'agent_uninstall')

    @router.post('/agent/enroll')
    async def enroll(request: Request):
        header = request.headers.get('authorization', '')
        scheme, separator, enrollment_token = header.partition(' ')
        if (not separator or scheme.lower() != 'bearer'
            or not re.fullmatch(r'rdi_[A-Za-z0-9_-]{43}', enrollment_token)):
            raise DevError('INSTALL_TICKET_INVALID', '安装凭据无效、已使用或已过期，请重新生成安装命令', 401)
        now = time.time()
        with store.lock, store.db:
            ticket = store.db.execute('SELECT * FROM agent_install_tickets WHERE token_hash=? AND expires>?',
                                      (digest(enrollment_token), now)).fetchone()
            if not ticket:
                raise DevError('INSTALL_TICKET_INVALID', '安装凭据无效、已使用或已过期，请重新生成安装命令', 401)
            device = store.db.execute('SELECT d.name,d.secret,d.enabled,s.active AS space_active,COALESCE(u.active,1) AS owner_active FROM devices d JOIN spaces s ON s.id=d.space_id LEFT JOIN iam_users u ON u.user_id=d.owner_user_id WHERE d.id=?', (ticket['device_id'],)).fetchone()
            if not device or not device['enabled'] or not device['space_active'] or not device['owner_active']:
                raise DevError('INSTALL_TICKET_INVALID', '设备已停用或被移除，请在面板重新确认', 401)
            secret = store.decrypt(device['secret'])
            if not hmac.compare_digest(digest(secret), ticket['secret_fingerprint']):
                raise DevError('INSTALL_TICKET_INVALID', '设备密钥已变化，请重新生成安装命令', 401)
            pairing = {'device_id': ticket['device_id'], 'name': device['name'], 'secret': secret, 'hub_url': ticket['hub_url']}
            store.db.execute('DELETE FROM agent_install_tickets WHERE token_hash=?', (digest(enrollment_token),))
            store.db.execute('INSERT INTO audit(at,actor,action,target,status,detail) VALUES (?,?,?,?,?,?)',
                             (now, 'installer', 'device.enrolled', ticket['device_id'], 'ok', '{}'))
        return JSONResponse(pairing, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    return router
