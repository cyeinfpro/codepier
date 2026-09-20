"""App-scoped bridge to an owner-installed Codex Computer Use MCP provider.

No model invocation, screen scraping fallback, OS permission changes, or native
binary redistribution. A CodePier lease is device-wide; other desktop software is
outside this lease and can still change the screen.
"""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import json
import os
import platform
import signal
import sqlite3
import time
import uuid
from pathlib import Path
from jsonschema import Draft202012Validator
from shared.computer_contracts import NATIVE_ACTIONS, NATIVE_TOOLS
from shared.computer_diagnostics import native_error
from shared.computer_media import MAX_CONTENT_BYTES, normalize_content, purge_database
from shared.util import DevError, VERSION, valid_json_value


def validate_computer(value):
    defaults = {'enabled': False, 'projects': [], 'allowed_apps': [], 'plugin_root': '', 'codex_home': '', 'app_server': '', 'transport': 'auto',
                'max_session_seconds': 900, 'observation_max_age_seconds': 60, 'call_timeout_seconds': 45,
                'approval_timeout_seconds': 60}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError('computer 必须是有效配置对象，不能包含未知字段')
    c = {**defaults, **value}
    if type(c['enabled']) is not bool:
        raise ValueError('computer.enabled 必须是 true/false')
    for key in ('projects', 'allowed_apps'):
        if not isinstance(c[key], list) or len(c[key]) > 100 or any(not isinstance(x, str) or not x.strip() or len(x) > 512 or '\x00' in x for x in c[key]):
            raise ValueError(f'computer.{key} 必须是明确的非空名称数组')
    if c['transport'] not in ('auto','direct-mcp','codex-app-server'):
        raise ValueError('computer.transport 无效')
    for key in ('plugin_root', 'codex_home', 'app_server'):
        if not isinstance(c[key], str) or '\x00' in c[key] or c[key] and not Path(c[key]).expanduser().is_absolute():
            raise ValueError(f'computer.{key} 必须为空或本机绝对路径')
    for key, low, high in [('max_session_seconds', 30, 1800), ('observation_max_age_seconds', 5, 120), ('call_timeout_seconds', 5, 90), ('approval_timeout_seconds', 5, 90)]:
        if type(c[key]) is not int or not low <= c[key] <= high:
            raise ValueError(f'computer.{key} 必须是 {low}–{high} 的整数')
    if c['enabled'] and (not c['projects'] or not c['allowed_apps']):
        raise ValueError('启用 Computer Use 必须显式设置 computer.projects 和 computer.allowed_apps；通配符 * 仅供本机管理员明确授权')
    return c


def discover_provider(c):
    home = Path(c['codex_home'] or os.getenv('CODEX_HOME') or str(Path.home() / '.codex')).expanduser().resolve()
    client = home / 'computer-use/Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient'
    if c['plugin_root']:
        candidates = [Path(c['plugin_root']).expanduser()]
    else:
        installed = Path('/Applications/Codex.app/Contents/Resources/plugins/openai-bundled/plugins/computer-use')
        cache = home / 'plugins/cache/openai-bundled/computer-use'
        candidates = [installed]
        if cache.is_dir():
            candidates += sorted((p for p in cache.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)[:20]
    root = next((p.resolve() for p in candidates if (p / 'bin/computer-use-client-launcher').is_file()
                 and os.access(p / 'bin/computer-use-client-launcher', os.X_OK)), None)
    version = None
    if root:
        try:
            manifest = root / '.codex-plugin/plugin.json'
            if manifest.stat().st_size < 65536:
                version = json.loads(manifest.read_text()).get('version')
        except (OSError, ValueError, AttributeError):
            pass
    return {'provider': 'codex-computer-use', 'platform': platform.system(), 'plugin_root': str(root) if root else None,
            'launcher': str(root / 'bin/computer-use-client-launcher') if root else None, 'version': version,
            'codex_home': str(home), 'native_client_present': client.is_file() and os.access(client, os.X_OK),
            'available': bool(root and (c['plugin_root'] or platform.system() == 'Darwin' and client.is_file())),
            'custom_local_provider': bool(c['plugin_root']),
            'app_server': c['app_server'] or '/Applications/Codex.app/Contents/Resources/codex',
            'transport': c['transport'] if c['transport'] != 'auto' else ('direct-mcp' if c['plugin_root'] else 'codex-app-server'), 'permissions': 'managed by native provider; not inferred from executable presence'}


def _schema_local(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {'$ref', '$dynamicRef'} and (not isinstance(child, str) or not child.startswith('#')):
                raise DevError('COMPUTER_SCHEMA', '不加载原生工具 Schema 的外部引用')
            _schema_local(child)
    elif isinstance(value, list):
        for child in value:
            _schema_local(child)


class NativeClient:
    """One newline-JSON MCP subprocess. Ambiguous requests are never retried."""
    def __init__(self, info, timeout=45):
        self.info, self.timeout = info, timeout
        self.process = None
        self.stderr_task = None
        self.lock = asyncio.Lock()
        self.counter = 0
        self.tools = {}
        self.server_info = {}
        self.closed = False

    async def start(self):
        if self.closed:
            raise DevError('COMPUTER_STOPPED', '原生连接已停止')
        if not self.info.get('available'):
            raise DevError('COMPUTER_PROVIDER_MISSING', '未找到可执行的本机 Codex Computer Use；请在本机安装/启用后重试')
        env = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'USER', 'TMPDIR', 'LANG', 'LC_ALL', 'XDG_RUNTIME_DIR', 'DISPLAY', 'SYSTEMROOT', 'SystemRoot'}}
        env['CODEX_HOME'] = self.info['codex_home']
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(self.info['launcher'], 'mcp',
            cwd=self.info['plugin_root'], env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8*1024*1024, start_new_session=os.name != 'nt'))
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                self.process = await spawning
                await self.close()
            raise
        if self.closed:
            await self.close()
            raise DevError('COMPUTER_STOPPED', '连接启动期间已停止')
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            init = await self.request('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                       'clientInfo': {'name': 'CodePier Computer Bridge', 'version': VERSION}})
            if init.get('protocolVersion') not in {'2025-03-26', '2025-06-18', '2025-11-25'}:
                raise DevError('COMPUTER_PROTOCOL', '原生 MCP 协议版本不兼容')
            self.server_info = init.get('serverInfo', {})
            await self.notify('notifications/initialized', {})
            cursor = None
            for _ in range(8):
                catalog = await self.request('tools/list', {'cursor': cursor} if cursor else {})
                tools = catalog.get('tools')
                if not isinstance(tools, list) or len(tools) > 64:
                    raise DevError('COMPUTER_PROTOCOL', '原生工具目录无效')
                for tool in tools:
                    if not isinstance(tool, dict) or tool.get('name') not in NATIVE_TOOLS:
                        continue
                    name, schema = tool['name'], tool.get('inputSchema')
                    if name in self.tools or not isinstance(schema, dict) or len(json.dumps(schema)) > 32000:
                        raise DevError('COMPUTER_SCHEMA', '原生工具目录重复或 Schema 无效')
                    _schema_local(schema)
                    try:
                        Draft202012Validator.check_schema(schema)
                    except Exception as exc:
                        raise DevError('COMPUTER_SCHEMA', '原生工具 Schema 无效；未启动桌面调用') from exc
                    self.tools[name] = {'name': name, 'inputSchema': schema}
                following = catalog.get('nextCursor')
                if not following:
                    break
                if not isinstance(following, str) or following == cursor or len(following) > 1024:
                    raise DevError('COMPUTER_PROTOCOL', '原生工具目录游标无效')
                cursor = following
            else:
                raise DevError('COMPUTER_PROTOCOL', '原生工具目录分页超限')
            if not {'get_app_state', 'list_apps'}.issubset(self.tools):
                raise DevError('COMPUTER_PROVIDER_INCOMPATIBLE', '原生提供方未提供 list_apps/get_app_state')
        except BaseException:
            await self.close()
            raise
        return self

    async def _drain_stderr(self):
        # Drain but never retain or publish application content, account information, or input.
        while await self.process.stderr.read(8192):
            pass

    async def notify(self, method, params):
        if self.closed or not self.process or self.process.returncode is not None:
            raise DevError('COMPUTER_DISCONNECTED', '本机桌面连接已断开')
        self.process.stdin.write((json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params})+'\n').encode())
        await asyncio.wait_for(self.process.stdin.drain(), 3)

    async def request(self, method, params):
        async with self.lock:
            if self.closed or not self.process or self.process.returncode is not None:
                raise DevError('COMPUTER_DISCONNECTED', '本机桌面连接已断开；不会自动重放输入')
            self.counter += 1
            request_id = self.counter
            try:
                async with asyncio.timeout(self.timeout):
                    self.process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}, ensure_ascii=False)+'\n').encode())
                    await self.process.stdin.drain()
                    for _ in range(256):
                        raw = await self.process.stdout.readline()
                        if not raw or len(raw) > 7*1024*1024:
                            raise DevError('COMPUTER_DISCONNECTED', '原生连接中断或响应超过限制；未重试')
                        message = json.loads(raw)
                        if not isinstance(message, dict) or not valid_json_value(message) or message.get('jsonrpc') != '2.0':
                            raise DevError('COMPUTER_PROTOCOL', '原生 MCP 响应无效')
                        if 'method' in message:
                            if 'id' in message:
                                self.process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32601, 'message': 'Client callbacks are not supported; use native permission UI'}})+'\n').encode())
                                await self.process.stdin.drain()
                            continue
                        if type(message.get('id')) is not int or message['id'] != request_id:
                            raise DevError('COMPUTER_PROTOCOL', '原生 MCP 响应编号不匹配')
                        if 'error' in message:
                            raise native_error(message['error'])
                        if not isinstance(message.get('result'), dict):
                            raise DevError('COMPUTER_PROTOCOL', '原生工具结果不是对象')
                        return message['result']
                    raise DevError('COMPUTER_PROTOCOL', '原生通知数量超限')
            except BaseException as exc:
                await self.close()
                if isinstance(exc, (DevError, asyncio.CancelledError)):
                    raise
                code = 'COMPUTER_TIMEOUT' if isinstance(exc, TimeoutError) else 'COMPUTER_PROTOCOL'
                raise DevError(code, '原生桌面请求未取得可确认结果；连接已停止，不自动重试') from exc

    def validate_call(self, name, args):
        if name not in NATIVE_TOOLS or name not in self.tools:
            raise DevError('COMPUTER_ACTION_UNAVAILABLE', '本机原生提供方未公布此桌面操作')
        errors = list(Draft202012Validator(self.tools[name]['inputSchema']).iter_errors(args))
        if errors:
            raise DevError('COMPUTER_SCHEMA_CHANGED', '动作与当前原生工具 Schema 不兼容；未发送输入，请核对版本')

    async def call(self, name, args):
        self.validate_call(name, args)
        return await self.request('tools/call', {'name': name, 'arguments': args})

    async def close(self):
        self.closed = True
        p = self.process
        if p and p.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                if os.name != 'nt':
                    os.killpg(p.pid, signal.SIGTERM)
                else:
                    p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    if os.name != 'nt':
                        os.killpg(p.pid, signal.SIGKILL)
                    else:
                        p.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(p.wait(), 2)
        if self.stderr_task and self.stderr_task is not asyncio.current_task():
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)


class Computer:
    def __init__(self, config_source, state_dir: Path, journal=None, client_factory=None, approvals=None):
        self.config_source = config_source
        self.state_dir = Path(state_dir)
        self.stop_file = self.state_dir / 'computer-use.stopped'
        self.journal = journal
        self.client_factory = client_factory
        self.approvals = approvals
        self.lock = asyncio.Lock()
        self.session = None
        self.generation = 0
        self.guard = None
        self.stopping = False

    def make_client(self, info, timeout, approval_timeout=60):
        if self.client_factory:
            client = self.client_factory(info, timeout)
        elif info.get('transport') == 'direct-mcp':
            client = NativeClient(info, timeout)
        else:
            from agent.computer_appserver import AppServerClient
            client = AppServerClient(info, timeout)
        client.approval_timeout_seconds = approval_timeout
        return client

    @staticmethod
    def phase(callback, stage, **metadata):
        # Diagnostics must not change whether a desktop request executes.
        if callback:
            with contextlib.suppress(Exception):
                callback(stage, **metadata)

    async def native(self, client, method, parameters, stage, phase):
        started = time.monotonic()
        approval_started = getattr(client, 'approval_wait_ms', 0)
        def timings():
            duration = max(0, round((time.monotonic()-started)*1000))
            approval = max(0, round(getattr(client, 'approval_wait_ms', 0)-approval_started))
            return {'duration_ms': duration, 'approval_wait_ms': approval,
                    'native_call_ms': max(0, duration-approval)}
        self.phase(phase, stage, outcome='started')
        try:
            result = await client.start() if method is None else await client.call(method, parameters)
        except (Exception, asyncio.CancelledError) as exc:
            exit_code = getattr(getattr(client, 'process', None), 'returncode', None)
            self.phase(phase, stage, outcome='cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed',
                       **timings(),
                       error_code=exc.code if isinstance(exc, DevError) else 'COMPUTER_CANCELLED' if isinstance(exc, asyncio.CancelledError) else 'COMPUTER_PROTOCOL',
                       **({'provider_exit_code': exit_code} if type(exit_code) is int else {}))
            raise
        self.phase(phase, stage, outcome='completed', **timings())
        return result

    async def approval(self, session, project, operation_id, message, not_after=None, phase=None):
        def valid():
            try:
                self._current(project, session['id'])
                self._still_current(session)
                return not session['client'].closed and (not_after is None or time.time() < not_after)
            except (DevError, ValueError):
                return False
        if not self.approvals or not operation_id or not valid():
            return {'action': 'cancel'}
        timeout = min(self.settings()['approval_timeout_seconds'], session['deadline']-time.monotonic())
        if not_after is not None:
            timeout = min(timeout, not_after-time.time())
        if timeout <= 0:
            return {'action': 'cancel'}
        context = {'session_id':session['id'], 'project_id':session['project'],
                   'owner':session['owner'], 'app':session['app'], 'operation_id':operation_id}
        started = time.monotonic()
        self.phase(phase, 'approval_wait', outcome='started', approval_timeout_seconds=timeout)
        decision = {'action': 'cancel'}
        try:
            decision = await self.approvals.request(context, message, valid, timeout)
            if not valid() or decision.get('action') not in {'accept', 'decline', 'cancel'}:
                decision = {'action': 'cancel'}
            return decision
        finally:
            self.phase(phase, 'approval_decided', outcome=decision.get('action', 'cancel'),
                       duration_ms=max(0, round((time.monotonic()-started)*1000)))

    def settings(self):
        try:
            return validate_computer(self.config_source().get('computer', {}))
        except (ValueError, TypeError, AttributeError, OSError) as exc:
            raise DevError('COMPUTER_CONFIG_INVALID', '本机桌面配置无效；已停止继续使用会话，请在本机修复配置', 403) from exc

    @staticmethod
    def signature(c):
        return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()

    def allowed(self, c, project):
        return '*' in c['projects'] or project.get('id') in c['projects'] or project.get('alias') in c['projects']

    def authorize(self, project):
        c = self.settings()
        if self.stopping or self.stop_file.exists():
            raise DevError('COMPUTER_STOPPED', 'Computer Use 已在本机紧急停止；需本机管理员明确恢复')
        if not c['enabled'] or not self.allowed(c, project):
            raise DevError('COMPUTER_DISABLED', '本机未为该项目授权 Computer Use；Shell/Skills 授权不会自动开启桌面控制', 403)
        if not isinstance(project.get('_computer_owner'), str) or not project['_computer_owner']:
            raise DevError('COMPUTER_OWNER_MISSING', 'Hub 未提供可信会话归属；请同时升级 Hub 与 Agent', 403)
        return c

    def start_guard(self):
        if not self.guard or self.guard.done():
            self.guard = asyncio.create_task(self._guard())

    async def _guard(self):
        count = 0
        while not self.stopping:
            await asyncio.sleep(1)
            s = self.session
            if s and self.revoked(s):
                await self._stop('expired_or_revoked')
            count += 1
            if self.journal is not None and count % 15 == 0:
                # Cleanup failures must not disable the session-expiry watchdog.
                try:
                    with self.journal.lock, self.journal.db:
                        purge_database(self.journal.db, 'calls')
                except (OSError, sqlite3.Error):
                    pass

    def status(self, project):
        c = self.settings()
        info = discover_provider(c)
        s = self.session
        own = bool(s and not self.revoked(s) and s['owner'] == project.get('_computer_owner')
                   and s['project'] == project.get('id') and s['root'] == project.get('root'))
        return {'enabled': c['enabled'] and self.allowed(c, project), 'locally_stopped': self.stop_file.exists(),
                'provider': info, 'session_active': bool(s), 'active_session_id': s['id'] if own else None,
                'active_session': {'session_id': s['id'], 'app': s['app'], 'expires_at': s['expires_at'],
                                   'remaining_seconds': max(0, round(s['deadline']-time.monotonic()))} if own else None,
                'supported_actions': sorted(NATIVE_ACTIONS), 'capabilities_verified': False,
                'scope_required': 'computer', 'screen_permissions_verified': False,
                'max_session_seconds': c['max_session_seconds'], 'observation_max_age_seconds': c['observation_max_age_seconds'],
                'call_timeout_seconds': c['call_timeout_seconds'], 'approval_timeout_seconds': c['approval_timeout_seconds'],
                'warning': 'Desktop access is not a project filesystem sandbox. OS/app authorization remains native. The CodePier lease does not lock out Codex or the human user.'}

    def _current(self, project, session_id):
        c = self.authorize(project)
        s = self.session
        if not s or s['id'] != session_id or s['owner'] != project['_computer_owner'] or s['project'] != project.get('id') or s['root'] != project.get('root'):
            raise DevError('COMPUTER_SESSION_NOT_FOUND', '找不到属于当前项目/授权的桌面会话', 404)
        if time.monotonic() >= s['deadline'] or s['config_signature'] != self.signature(c):
            raise DevError('COMPUTER_SESSION_EXPIRED', '桌面会话已过期或本机权限已变化，请重新建立会话', 409)
        return s, c

    def revoked(self, s):
        try:
            return (time.monotonic() >= s['deadline'] or self.stop_file.exists() or s['client'].closed
                    or s['config_signature'] != self.signature(self.settings()))
        except DevError:
            # A malformed hot update cannot kill the watchdog and leave a lease live.
            return True

    def _still_current(self, s):
        if self.session is not s or self.revoked(s):
            raise DevError('COMPUTER_STOPPED', '会话已停止、过期或撤权；不继续发出输入')

    async def _stop(self, reason):
        self.generation += 1
        old, self.session = self.session, None
        if old:
            old['observation'] = None
            if self.approvals:
                self.approvals.cancel(old['id'])
            await old['client'].close()
        return {'closed': True, 'reason': reason, 'session_id': old['id'] if old else None,
                'note': '仅停止 CodePier 发出的后续控制。不会关闭用户应用，不撤销已经发生的点击/输入，也不声称停止其他 Codex 会话。'}

    async def _read_state(self, s, stage, phase):
        try:
            result = normalize_content(await self.native(s['client'], 'get_app_state', {'app': s['app']}, stage, phase))
            self._still_current(s)
            return result
        except (DevError, asyncio.CancelledError):
            if self.session is s:
                await self._stop('native_read_failed')
            raise

    async def _observe(self, s, phase=None):
        self._still_current(s)
        s['observation'] = None
        result = await self._read_state(s, 'native_observe', phase)
        result.update({'session_id': s['id'], 'app': s['app'], 'observation_id': None, 'session_expires_at': s['expires_at']})
        if not result['native_is_error'] and not result['content_truncated'] and (result['text'].strip() or result['images']):
            observation_id = uuid.uuid4().hex
            s['observation'] = {'id': observation_id, 'at': time.monotonic(), 'fingerprint': result['state_fingerprint'], 'images': result['images']}
            result['observation_id'] = observation_id
            result['observed_at'] = time.time()
        return result

    @staticmethod
    def coordinates(action, images):
        pairs = [('x', 'y')] if action['type'] == 'click' and action.get('x') is not None else [('from_x', 'from_y'), ('to_x', 'to_y')] if action['type'] == 'drag' else []
        if pairs:
            if len(images) != 1:
                raise DevError('COMPUTER_COORDINATES', '坐标输入需要单张已确认尺寸的截图；可改用辅助功能元素')
            for x, y in pairs:
                if not (0 <= action[x] < images[0]['width'] and 0 <= action[y] < images[0]['height']):
                    raise DevError('COMPUTER_COORDINATES', '坐标超出当前截图像素范围；未发出输入')

    async def execute(self, tool, project, args, not_after=None, operation_id=None, phase=None):
        self.start_guard()
        if self.session and self.revoked(self.session):
            await self._stop('expired_or_revoked')
        if tool == 'computer_status' and not args.get('probe'):
            return self.status(project)
        if tool == 'computer_session_close':
            # Deliberately bypass the action lock so STOP can interrupt a blocked native RPC.
            if args.get('force'):
                if not project.get('_computer_admin'):
                    raise DevError('COMPUTER_FORCE_DENIED', '只有面板管理员可以强制停止其他授权的会话', 403)
            else:
                s = self.session
                if not s:
                    return {'closed': True, 'session_id': args['session_id'], 'note': '会话已经停止'}
                if s['id'] != args['session_id'] or s['owner'] != project.get('_computer_owner') or s['project'] != project.get('id') or s['root'] != project.get('root'):
                    raise DevError('COMPUTER_SESSION_NOT_FOUND', '会话不属于当前项目/授权', 404)
            return await self._stop('owner_closed')
        c = self.authorize(project)
        async with self.lock:
            if not_after is not None and time.time() > not_after:
                raise DevError("QUEUE_EXPIRED", "桌面请求已过首次执行期限；未发出输入")
            c = self.authorize(project)
            if self.session and (time.monotonic() >= self.session['deadline'] or self.session['config_signature'] != self.signature(c)):
                await self._stop('expired_or_revoked')
            if tool in {'computer_status', 'computer_apps'}:
                generation = self.generation
                info = discover_provider(c)
                client = self.make_client(info, c['call_timeout_seconds'], c['approval_timeout_seconds'])
                try:
                    await self.native(client, None, None, 'native_startup', phase)
                    self.authorize(project)
                    if self.generation != generation:
                        raise DevError('COMPUTER_STOPPED', '检查接口期间桌面会话已停止')
                    if tool == 'computer_status':
                        return {**self.status(project), 'capabilities_verified': True, 'native_tools': sorted(client.tools),
                                'missing_actions': sorted(NATIVE_ACTIONS - set(client.tools)), 'server_info': client.server_info}
                    result = normalize_content(await client.call('list_apps', {}))
                    result['note'] = '原生列表可能包含近期使用记录；这里只列举应用，不授予应用访问权限。'
                    return result
                finally:
                    await client.close()
            if tool == 'computer_session_open':
                if self.session:
                    raise DevError('COMPUTER_BUSY', '这台设备已有 CodePier 桌面会话；请先结束原会话或在面板紧急停止', 409)
                if '*' not in c['allowed_apps'] and args['app'] not in c['allowed_apps']:
                    raise DevError('COMPUTER_APP_DENIED', '应用未在本机 computer.allowed_apps 中明确授权', 403)
                generation = self.generation
                client = self.make_client(discover_provider(c), c['call_timeout_seconds'], c['approval_timeout_seconds'])
                try:
                    await self.native(client, None, None, 'native_startup', phase)
                    if generation != self.generation or self.signature(self.authorize(project)) != self.signature(c):
                        raise DevError('COMPUTER_STOPPED', '会话建立期间已停止或撤权')
                    ttl = min(args['ttl_seconds'], c['max_session_seconds'])
                    s = {'id': uuid.uuid4().hex, 'owner': project['_computer_owner'], 'project': project.get('id'),
                         'root': project.get('root'), 'app': args['app'], 'client': client, 'expires_at': time.time()+ttl,
                         'deadline': time.monotonic()+ttl, 'config_signature': self.signature(c), 'observation': None}
                    self.session = s
                    return {'session_id': s['id'], 'app': s['app'], 'expires_at': s['expires_at'],
                            'supported_actions': sorted(NATIVE_ACTIONS & set(client.tools)), 'native_tools': sorted(client.tools),
                            'missing_actions': sorted(NATIVE_ACTIONS - set(client.tools)),
                            'next': 'computer_observe', 'note': '会话已预留；首次读屏仍由原生程序检查录屏、辅助功能及应用权限。'}
                except BaseException:
                    await client.close()
                    raise
            s, c = self._current(project, args['session_id'])
            s['client'].approval_handler = lambda message: self.approval(s, project, operation_id, message,
                not_after=not_after if tool == 'computer_action' else None, phase=phase)
            if tool == 'computer_observe':
                return await self._observe(s, phase)
            if tool != 'computer_action':
                raise DevError('UNKNOWN_TOOL', '未知桌面工具')
            previous = s['observation']
            if not previous or previous['id'] != args['observation_id'] or time.monotonic()-previous['at'] > c['observation_max_age_seconds']:
                raise DevError('COMPUTER_STALE_OBSERVATION', '屏幕依据过期、已消费或已被新观察替换；先调用 computer_observe', 409)
            action = args['action']
            self.coordinates(action, previous['images'])
            name = action['type']
            parameters = {k: v for k, v in action.items() if k != 'type' and v is not None}
            parameters['app'] = s['app']  # Never take an app/command from action arguments.
            s['client'].validate_call(name, parameters)
            s['observation'] = None
            if args.get('verify_unchanged', True):
                current = await self._read_state(s, 'native_preflight', phase)
                if not_after is not None and time.time() >= not_after:
                    raise DevError('QUEUE_EXPIRED', '确认界面期间输入请求已过期；未发出输入',
                                   next='computer_observe', input_sent=False)
                if current['native_is_error'] or current['content_truncated'] or current['state_fingerprint'] != previous['fingerprint']:
                    raise DevError('COMPUTER_SCREEN_CHANGED', '应用内容已变化或无法确认；未发出输入，请重新观察', 409,
                                   next='computer_observe', input_sent=False)
            self._still_current(s)
            if not_after is not None and time.time() > not_after:
                raise DevError("QUEUE_EXPIRED", "确认界面期间输入请求已过期；未发出输入")
            try:
                raw = await self.native(s['client'], name, parameters, 'native_action', phase)
                action_result = normalize_content(raw)
                if action_result['native_is_error']:
                    return {**action_result, 'session_id': s['id'], 'observation_id': None, 'action_outcome': 'uncertain',
                            'note': '原生动作报错，可能已有部分效果；不要盲目重复输入，先重新观察。'}
            except (DevError, asyncio.CancelledError) as exc:
                # Journal already records this operation as started. Never reconnect-and-repeat input.
                if self.session is s:
                    await self._stop('uncertain_action')
                raise DevError('COMPUTER_ACTION_UNCERTAIN', '桌面输入未取得可确认结果；会话已停止。检查实际界面后再决定下一步，不要换键盲目重发', 409) from exc
            try:
                # The first-input deadline fences new input, not observation after its receipt.
                s['client'].approval_handler = lambda message: self.approval(s, project, operation_id, message, phase=phase)
                observed = await self._observe(s, phase)
                return {**observed, 'action_outcome': 'completed', 'action_type': name,
                        'note': '动作已获原生回执。新的 observation_id 来自动作后的重新读屏；不是业务操作成功证明。'}
            except DevError as exc:
                return {'session_id': s['id'], 'observation_id': None, 'action_outcome': 'completed', 'action_type': name,
                        'observation_error': exc.code, 'session_closed': self.session is not s,
                        'next': 'computer_session_open' if self.session is not s else 'computer_observe',
                        'note': '动作已获回执，但后续读屏失败；不要重复动作。会话停止后请重新连接，再读取实际界面。'}

    async def close(self):
        self.stopping = True
        await self._stop('agent_stopped')
        if self.guard and self.guard is not asyncio.current_task():
            self.guard.cancel()
            await asyncio.gather(self.guard, return_exceptions=True)
