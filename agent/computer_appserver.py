"""Isolated Codex App Server transport; no model turns or copied credentials."""
import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from jsonschema import Draft202012Validator
from agent.computer import NativeClient, _schema_local
from shared.computer_contracts import NATIVE_TOOLS
from shared.util import DevError, VERSION, valid_json_value
from shared.computer_diagnostics import native_error

class AppServerClient(NativeClient):
    def __init__(self, info, timeout=45):
        super().__init__(info, timeout)
        self.temporary = None
        self.thread_id = None
        self.approval_handler = None
        self.approval_timeout_seconds = 60
        self.approval_wait_ms = 0

    async def start(self):
        if self.closed or not self.info.get('available'):
            raise DevError('COMPUTER_PROVIDER_MISSING', '本机原生桌面接口不可用')
        binary = self.info.get('app_server')
        if not binary or not Path(binary).is_file():
            raise DevError('COMPUTER_PROVIDER_MISSING', '未找到本机 Codex App Server，请更新 Codex')
        self.temporary = tempfile.TemporaryDirectory(prefix='codepier-computer-appserver-')
        home = Path(self.temporary.name)
        # This home contains no user auth.json, history, skills, or other MCP servers.
        config = ('[mcp_servers.codepier_computer]\ncommand = '+json.dumps(self.info['launcher'])+
                  '\nargs = ["mcp"]\nstartup_timeout_sec = 30\ntool_timeout_sec = '+str(int(self.timeout+self.approval_timeout_seconds+5))+'\n'+
                  '[mcp_servers.codepier_computer.env]\nCODEX_HOME = '+json.dumps(self.info['codex_home'])+'\n')
        (home/'config.toml').write_text(config)
        env = {k:v for k,v in os.environ.items() if k in {'PATH','HOME','USER','TMPDIR','LANG','LC_ALL'}}
        env['CODEX_HOME'] = str(home)
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(binary, 'app-server',
            cwd=str(home), env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8*1024*1024, start_new_session=os.name != 'nt'))
        try:
            try:
                self.process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                self.process = await spawning
                raise
            if self.closed:
                raise DevError('COMPUTER_STOPPED', '连接启动期间已停止')
            self.stderr_task = asyncio.create_task(self._drain_stderr())
            init = await self.request('initialize', {'clientInfo':{'name':'codepier_computer','version':VERSION},
                                                   'capabilities':{'experimentalApi':True}})
            self.server_info = {'name':'codex-app-server','userAgent':init.get('userAgent')}
            await self.notify('initialized', {})
            thread = await self.request('thread/start', {'cwd':str(home),'ephemeral':True,
                                                        'approvalPolicy':'untrusted','sandbox':'read-only'})
            native_thread = thread.get('thread')
            if not isinstance(native_thread, dict) or not isinstance(native_thread.get('id'), str) or not 0 < len(native_thread['id']) <= 200:
                raise DevError('COMPUTER_PROTOCOL', 'App Server 未返回有效临时会话编号')
            self.thread_id = native_thread['id']
            cursor = None
            for _ in range(8):
                result = await self.request('mcpServerStatus/list', {'threadId':self.thread_id,'limit':100,
                    **({'cursor':cursor} if cursor else {})})
                servers = result.get('data')
                if not isinstance(servers, list) or len(servers) > 100:
                    raise DevError('COMPUTER_PROTOCOL', 'App Server 工具目录格式无效')
                for server in servers:
                    if not isinstance(server, dict):
                        raise DevError('COMPUTER_PROTOCOL', 'App Server 工具目录项无效')
                    if server.get('name') != 'codepier_computer':
                        continue
                    catalog = server.get('tools', {})
                    if not isinstance(catalog, dict) or len(catalog)>64:
                        raise DevError('COMPUTER_SCHEMA','原生工具目录无效')
                    for tool in catalog.values():
                        if not isinstance(tool, dict):
                            raise DevError('COMPUTER_SCHEMA', '原生工具目录项无效')
                        name, schema = tool.get('name'), tool.get('inputSchema')
                        if name not in NATIVE_TOOLS:
                            continue
                        if name in self.tools or not isinstance(schema,dict) or len(json.dumps(schema))>32000:
                            raise DevError('COMPUTER_SCHEMA','原生工具 Schema 无效')
                        _schema_local(schema)
                        try:
                            Draft202012Validator.check_schema(schema)
                        except Exception as exc:
                            raise DevError('COMPUTER_SCHEMA', '原生工具 Schema 无效') from exc
                        self.tools[name] = {'name':name,'inputSchema':schema}
                following=result.get('nextCursor')
                if not following:break
                if not isinstance(following,str) or following==cursor or len(following)>1024:raise DevError('COMPUTER_PROTOCOL','目录游标无效')
                cursor=following
            else:
                raise DevError('COMPUTER_PROTOCOL', '目录分页超限')
            if not {'list_apps','get_app_state'}.issubset(self.tools):
                raise DevError('COMPUTER_PROVIDER_INCOMPATIBLE','未发现原生读屏接口')
            return self
        except BaseException:
            await self.close()
            raise

    async def request(self, method, params):
        async with self.lock:
            if self.closed or not self.process or self.process.returncode is not None:
                raise DevError('COMPUTER_DISCONNECTED','本机桌面连接已停止')
            self.counter += 1
            identifier = self.counter
            approval_budget = self.approval_timeout_seconds
            callback_ids = set()
            try:
                async with asyncio.timeout(self.timeout) as rpc_timeout:
                    self.process.stdin.write((json.dumps({'id':identifier,'method':method,'params':params})+'\n').encode())
                    await self.process.stdin.drain()
                    for _ in range(256):
                        raw = await self.process.stdout.readline()
                        if not raw or len(raw)>7*1024*1024:
                            raise DevError('COMPUTER_DISCONNECTED','App Server 断开或返回超限')
                        data = json.loads(raw)
                        if not isinstance(data,dict) or not valid_json_value(data):
                            raise DevError('COMPUTER_PROTOCOL','App Server 返回无效数据')
                        if 'method' in data:
                            if 'id' in data:
                                callback_id = data['id']
                                if (type(callback_id) not in (str, int) or isinstance(callback_id, str) and not 0 < len(callback_id) <= 200
                                    or callback_id in callback_ids):
                                    raise DevError('COMPUTER_PROTOCOL', 'App Server 回调编号无效或重复')
                                callback_ids.add(callback_id)
                                response = {'id':data['id'],'error':{'code':-32601,'message':'Unsupported callback'}}
                                p=data.get('params',{})
                                if data['method']=='mcpServer/elicitation/request':
                                    decision={'action':'cancel'}
                                    # Only the observed empty application-consent form is supported.
                                    # URL/credential/arbitrary input forms never become automatic approvals.
                                    schema=p.get('requestedSchema') if isinstance(p,dict) else None
                                    if (isinstance(p,dict) and p.get('threadId')==self.thread_id and self.thread_id
                                        and p.get('serverName')=='codepier_computer' and p.get('mode')=='form'
                                        and isinstance(schema,dict) and schema.get('type')=='object'
                                        and schema.get('properties')=={} and ('required' not in schema or schema['required']==[])
                                        and ('$schema' not in schema or isinstance(schema['$schema'],str))
                                        and set(schema)<= {'type','properties','required','$schema'}
                                        and isinstance(p.get('message'),str) and 0<len(p['message'])<=2000
                                        and self.approval_handler and approval_budget > 0):
                                        # Human thinking time uses its own bounded budget. Keep the
                                        # remaining native RPC time; repeated callbacks cannot extend it.
                                        loop = asyncio.get_running_loop()
                                        remaining = rpc_timeout.when()-loop.time()
                                        if remaining <= 0:
                                            raise TimeoutError()
                                        rpc_timeout.reschedule(None)
                                        start = loop.time()
                                        try:
                                            async with asyncio.timeout(approval_budget):
                                                decision=await self.approval_handler(p['message'])
                                        except TimeoutError:
                                            decision={'action':'cancel'}
                                        finally:
                                            waited=loop.time()-start
                                            self.approval_wait_ms += max(0, round(waited*1000))
                                            approval_budget=max(0, approval_budget-waited)
                                            rpc_timeout.reschedule(loop.time()+remaining)
                                        if not isinstance(decision, dict) or decision.get('action') not in {'accept','decline','cancel'}:
                                            decision={'action':'cancel'}
                                        else:
                                            action=decision['action']
                                            decision={'action':action, **({'content':{}} if action=='accept' else {})}
                                    response={'id':data['id'],'result':decision}
                                self.process.stdin.write((json.dumps(response)+'\n').encode())
                                await self.process.stdin.drain()
                            continue
                        if type(data.get('id')) is not int or data['id']!=identifier:
                            raise DevError('COMPUTER_PROTOCOL','App Server 返回编号不匹配')
                        if 'error' in data:
                            raise native_error(data['error'])
                        if not isinstance(data.get('result'),dict):
                            raise DevError('COMPUTER_PROTOCOL','App Server 结果格式无效')
                        return data['result']
                    raise DevError('COMPUTER_PROTOCOL','App Server 通知数量超限')
            except BaseException as exc:
                await self.close()
                if isinstance(exc,(DevError,asyncio.CancelledError)):raise
                raise DevError('COMPUTER_TIMEOUT' if isinstance(exc,TimeoutError) else 'COMPUTER_PROTOCOL',
                               '原生请求未取得确认结果；连接已停止，不自动重试') from exc

    async def call(self,name,args):
        self.validate_call(name,args)
        return await self.request('mcpServer/tool/call',{'threadId':self.thread_id,'server':'codepier_computer','tool':name,'arguments':args})

    async def close(self):
        await super().close()
        if self.temporary:
            self.temporary.cleanup()
            self.temporary=None
