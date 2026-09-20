"""Private loopback bridge for owner controls and Chrome native messaging.

No credentials in URLs, no browser CORS, no forwarded or non-loopback requests.
The descriptor is mode 0600 and belongs to one running Agent instance.
"""
from __future__ import annotations
import asyncio,contextlib,hmac,json,os,secrets,time,uuid
from pathlib import Path
from shared.util import DevError,atomic_json,valid_json_value

class LocalServer:
    def __init__(self,agent,browser):
        self.agent,self.browser=agent,browser;self.token=secrets.token_urlsafe(48)
        self.server=None;self.port=0;self.tasks=set();self.descriptor=agent.state_dir/'integration-local.json';self.instance=uuid.uuid4().hex
        self.native_polls=0

    async def start(self):
        self.server=await asyncio.start_server(self.client,'127.0.0.1',0,limit=2*1024*1024)
        self.port=self.server.sockets[0].getsockname()[1]
        atomic_json(self.descriptor,{'port':self.port,'token':self.token,'instance':self.instance,'pid':os.getpid(),'protocol':1})

    async def client(self,reader,writer):
        task=asyncio.current_task();self.tasks.add(task);status=200;data={}
        try:
            peer=writer.get_extra_info('peername')
            if not peer or peer[0]!='127.0.0.1':raise DevError('LOCAL_ONLY','仅允许本机直连',403)
            async with asyncio.timeout(5):header=await reader.readuntil(b'\r\n\r\n')
            if len(header)>8192:raise DevError('INVALID_HTTP','请求头过长',400)
            lines=header.decode('ascii').split('\r\n');method,path,version=lines[0].split(' ')
            headers={}
            for line in lines[1:]:
                if not line:continue
                key,value=line.split(':',1);key=key.lower()
                if key in headers:raise DevError('INVALID_HTTP','不接受重复请求头',400)
                headers[key]=value.strip()
            if method!='POST' or version!='HTTP/1.1' or headers.get('host')!=f'127.0.0.1:{self.port}':raise DevError('LOCAL_ONLY','本机请求目标无效',403)
            if any(k in headers for k in ('origin','forwarded','x-forwarded-for','x-forwarded-host','transfer-encoding')):
                raise DevError('LOCAL_ONLY','不接受网页或代理转发的控制请求',403)
            auth=headers.get('authorization','')
            if not hmac.compare_digest(auth.encode(),('Bearer '+self.token).encode()):raise DevError('LOCAL_AUTH','本机凭据无效',401)
            if headers.get('content-type')!='application/json':raise DevError('INVALID_HTTP','需要 JSON 请求',415)
            try:size=int(headers.get('content-length','0'))
            except ValueError as exc:raise DevError('INVALID_HTTP','长度无效',400) from exc
            if not 0<size<=1024*1024:raise DevError('BODY_TOO_LARGE','请求体超过本机桥接上限',413)
            async with asyncio.timeout(5):body=json.loads(await reader.readexactly(size))
            if not isinstance(body,dict) or not valid_json_value(body):raise DevError('INVALID_JSON','请求格式无效')
            data=await self.route(path,body)
        except DevError as exc:status=exc.status;data={'error':{'code':exc.code,'message':exc.message}}
        except (ValueError,UnicodeError,asyncio.IncompleteReadError,asyncio.LimitOverrunError,asyncio.TimeoutError):status=400;data={'error':{'code':'INVALID_HTTP','message':'本机请求格式无效或超时'}}
        except Exception:status=500;data={'error':{'code':'LOCAL_ERROR','message':'本机服务未确认结果，请核查原任务'}}
        try:
            raw=json.dumps(data,ensure_ascii=False).encode()
            writer.write(f'HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'.encode()+raw)
            await writer.drain()
        except (OSError,ConnectionError):pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):await writer.wait_closed()
            self.tasks.discard(task)

    async def route(self,path,body):
        if path=='/native/poll':
            if self.native_polls>=2:raise DevError('BROWSER_BUSY','已有浏览器宿主在等待',429)
            self.native_polls+=1
            try:return await self.browser.poll(body)
            finally:self.native_polls-=1
        if path=='/native/reply':return self.browser.reply(body)
        if path not in {'/status','/control'}:raise DevError('NOT_FOUND','未知本机入口',404)
        if not self.agent.config.get('integrations',{}).get('local_control'):raise DevError('LOCAL_CONTROL_DISABLED','本机控制入口未启用',403)
        projects=self.agent.integrations.known_projects()
        if path=='/status':return {'protocol':1,'instance':self.instance,'projects':[{'id':p['id'],'alias':p['alias'],'admission':self.agent.integrations.control.state(p)} for p in projects]}
        project=next((p for p in projects if p['id']==body.get('project') or p['alias']==body.get('project')),None)
        if not project:raise DevError('PROJECT_NOT_KNOWN','本机没有此项目的已认证映射记录',404)
        self.agent.engine.root(project)
        args={'project':project['alias'],'workspace_id':'','idempotency_key':body.get('idempotency_key'),
              'action':body.get('action'),'confirm':body.get('confirm',''),'include_native':body.get('include_native',False)}
        from shared.integration_contracts import Control
        args=Control.model_validate(args).model_dump()
        # Local requests also get a journal receipt; a lost response never repeats stop.
        identifier=__import__('hashlib').sha256(('local-control:'+args['idempotency_key']).encode()).hexdigest()[:32]
        owner={**project,'_integration_admin':True,'_integration_owner':'local-owner',
               '_coding_device':project['device_id'],'_execution_policy':{'origin':'panel'}}
        prior=self.agent.journal.start(identifier,{'tool':'integration_control','project':owner,'args':args})
        if prior is not None:return prior
        try:
            self.agent.journal.mark_running(identifier)
            result={'ok':True,'data':await self.agent.integrations.control.action(identifier,owner,args)}
        except DevError as exc:result={'ok':False,'error':{'code':exc.code,'message':exc.message}}
        self.agent.journal.finish(identifier,result);self.agent.journal.ack(identifier)
        return result

    async def close(self):
        if self.server:self.server.close();await self.server.wait_closed()
        for task in list(self.tasks):task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        with contextlib.suppress(OSError,ValueError):
            if json.loads(self.descriptor.read_text()).get('instance')==self.instance:self.descriptor.unlink()
