"""Bounded real LSP stdio client. No fallback pretending text search is semantics.

Servers are configured by the local owner, never supplied by a tool argument.
Each query owns its process group and closes it, including on timeout/cancellation.
"""
from __future__ import annotations
import asyncio,contextlib,hashlib,json,os,shutil,time
from pathlib import Path
from urllib.parse import urlsplit,unquote
from shared.execution_policy import enforce_argv
from shared.util import DevError,valid_json_value

LANGUAGES={'.py':'python','.js':'javascript','.mjs':'javascript','.cjs':'javascript','.jsx':'javascriptreact',
           '.ts':'typescript','.mts':'typescript','.cts':'typescript','.tsx':'typescriptreact',
           '.rs':'rust','.go':'go','.java':'java','.c':'c','.h':'c','.cpp':'cpp','.vue':'vue'}
MAX_MESSAGE=4*1024*1024


def selected(config,project,language):
    servers=config.get('integrations',{}).get('language_servers',{})
    spec=servers.get(language)
    if spec is None and isinstance(language,str) and language.endswith('react'):spec=servers.get(language[:-5])
    if not spec or not spec.get('enabled',True):return None
    projects=spec.get('projects',[])
    if '*' not in projects and project.get('alias') not in projects and project.get('id') not in projects:return None
    return spec


def status(config,project):
    rows=[]
    for language in sorted(config.get('integrations',{}).get('language_servers',{})):
        spec=selected(config,project,language)
        if spec:
            executable=shutil.which(spec['command'][0])
            rows.append({'language':language,'configured':True,'executable_available':bool(executable),
                         'protocol_verified':False,'reason':'本次只探测程序存在，未执行协议查询' if executable else '语言服务程序未找到'})
    return {'servers':rows,'starts_process':False,'semantic_queries_available':any(r['executable_available'] for r in rows),
            'column_unit':'one-based Unicode scalar','note':'查询会启动配置的语言服务；不是操作系统沙箱。'}


class Peer:
    def __init__(self,agent,identifier,project,root,spec):
        self.agent,self.identifier,self.project,self.root,self.spec=agent,identifier,project,root,spec
        self.process=None;self.reader=None;self.stderr=None;self.counter=0;self.pending={};self.diagnostics={};self.changed=asyncio.Event()
        self.notifications=0;self.error=None

    async def start(self):
        env={k:v for k,v in os.environ.items() if k in {'PATH','HOME','USER','USERPROFILE','SYSTEMROOT','SystemRoot','TEMP','TMP','TMPDIR','LANG','LC_ALL'}}
        env.update(self.spec.get('env',{}));command=self.spec['command']
        enforce_argv(self.agent.config,self.project,command,self.root,env)
        kwargs={'start_new_session':True} if os.name!='nt' else {'creationflags':__import__('subprocess').CREATE_NEW_PROCESS_GROUP}
        spawning=asyncio.create_task(asyncio.create_subprocess_exec(*command,cwd=self.root,env=env,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=MAX_MESSAGE+8192,**kwargs))
        try:self.process=await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self.process=await spawning;await self.close();raise
        except OSError as exc:raise DevError('LSP_UNAVAILABLE','无法启动已配置语言服务',409) from exc
        self.agent.processes[self.identifier]=self.process
        self.reader=asyncio.create_task(self.read_loop());self.stderr=asyncio.create_task(self.drain_stderr())
        response=await self.request('initialize',{'processId':os.getpid(),'rootUri':self.root.as_uri(),
            'workspaceFolders':[{'uri':self.root.as_uri(),'name':self.root.name}],
            'capabilities':{'general':{'positionEncodings':['utf-16']},'workspace':{'configuration':True,'workspaceFolders':True,'applyEdit':False},
              'textDocument':{'publishDiagnostics':{'versionSupport':True},'hover':{'contentFormat':['plaintext','markdown']},
                              'documentSymbol':{'hierarchicalDocumentSymbolSupport':True},'diagnostic':{}}},
            'initializationOptions':self.spec.get('initialization_options',{})})
        if not isinstance(response,dict):raise DevError('LSP_PROTOCOL','initialize 返回类型无效')
        capabilities=response.get('capabilities',{})
        if not isinstance(capabilities,dict) or capabilities.get('positionEncoding','utf-16')!='utf-16':
            raise DevError('LSP_ENCODING','语言服务未使用协商的 UTF-16 坐标')
        await self.notify('initialized',{})
        if self.spec.get('settings'):await self.notify('workspace/didChangeConfiguration',{'settings':self.spec['settings']})
        return capabilities

    async def send(self,message):
        if not self.process or self.process.returncode is not None:raise DevError('LSP_DISCONNECTED','语言服务已退出',409)
        body=json.dumps(message,ensure_ascii=False).encode()
        if len(body)>MAX_MESSAGE:raise DevError('LSP_LIMIT','语言服务请求超过大小限制')
        self.process.stdin.write(f'Content-Length: {len(body)}\r\n\r\n'.encode()+body)
        await self.process.stdin.drain()

    async def notify(self,method,params):await self.send({'jsonrpc':'2.0','method':method,'params':params})

    async def request(self,method,params):
        if self.error:raise self.error
        self.counter+=1;identifier=self.counter;future=asyncio.get_running_loop().create_future();self.pending[identifier]=future
        try:
            await self.send({'jsonrpc':'2.0','id':identifier,'method':method,'params':params})
            return await asyncio.wait_for(future,self.spec.get('timeout_seconds',20))
        except asyncio.TimeoutError as exc:raise DevError('LSP_TIMEOUT','语言服务未在期限内返回；没有伪造空结果',409) from exc
        finally:self.pending.pop(identifier,None)

    async def read_loop(self):
        try:
            while True:
                header=await self.process.stdout.readuntil(b'\r\n\r\n')
                if len(header)>8192:raise DevError('LSP_PROTOCOL','语言服务头部过长')
                lengths=[l.split(b':',1)[1].strip() for l in header.split(b'\r\n') if l.lower().startswith(b'content-length:')]
                if len(lengths)!=1:raise DevError('LSP_PROTOCOL','语言服务长度头无效')
                try:length=int(lengths[0])
                except ValueError as exc:raise DevError('LSP_PROTOCOL','无效消息长度') from exc
                if not 0<length<=MAX_MESSAGE:raise DevError('LSP_LIMIT','语言服务响应超过 4 MiB')
                message=json.loads(await self.process.stdout.readexactly(length))
                if not isinstance(message,dict) or not valid_json_value(message):raise DevError('LSP_PROTOCOL','语言服务返回无效 JSON')
                if 'method' in message:
                    self.notifications+=1
                    if self.notifications>20000:raise DevError('LSP_LIMIT','语言服务通知超过预算')
                    if 'id' in message:await self.server_request(message)
                    elif message['method']=='textDocument/publishDiagnostics':
                        p=message.get('params',{})
                        if isinstance(p,dict) and isinstance(p.get('uri'),str) and isinstance(p.get('diagnostics'),list):
                            self.diagnostics[p['uri']]=p;self.changed.set()
                elif type(message.get('id')) is int and message['id'] in self.pending:
                    future=self.pending[message['id']]
                    if not future.done():
                        if 'error' in message:
                            err=message['error']
                            code='LSP_UNSUPPORTED' if isinstance(err,dict) and err.get('code')==-32601 else 'LSP_QUERY_FAILED'
                            future.set_exception(DevError(code,'语言服务拒绝该查询；没有退回同名文本匹配',409))
                        else:future.set_result(message.get('result'))
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.error=exc if isinstance(exc,DevError) else DevError('LSP_DISCONNECTED','语言服务流中断或协议无效',409)
            for f in self.pending.values():
                if not f.done():f.set_exception(self.error)
            self.changed.set()

    async def server_request(self,message):
        method=message['method'];p=message.get('params',{});reply={'jsonrpc':'2.0','id':message['id']}
        if method=='workspace/configuration':
            settings=self.spec.get('settings',{});items=p.get('items',[]) if isinstance(p,dict) else []
            values=[]
            for item in items[:100]:
                value=settings
                for key in (item.get('section') or '').split('.'):
                    if key:value=value.get(key) if isinstance(value,dict) else None
                values.append(value)
            reply['result']=values
        elif method=='workspace/workspaceFolders':reply['result']=[{'uri':self.root.as_uri(),'name':self.root.name}]
        elif method=='workspace/applyEdit':reply['result']={'applied':False,'failureReason':'CodePier semantic navigation does not apply server edits'}
        elif method in {'window/workDoneProgress/create','client/registerCapability','client/unregisterCapability'}:reply['result']=None
        else:reply['error']={'code':-32601,'message':'Unsupported read-only client callback'}
        await self.send(reply)

    async def await_source_diagnostics(self, uri, capabilities):
        """Do not query a cold workspace index before the seed was analyzed.

        A positive protocol observation is required, never an arbitrary sleep or
        an empty workspace/symbol result treated as a completed index scan.
        """
        if capabilities.get('diagnosticProvider'):
            result=await self.request('textDocument/diagnostic',{'textDocument':{'uri':uri}})
            if not isinstance(result,dict) or result.get('kind')!='full':
                raise DevError('LSP_NOT_READY','语言服务尚未确认当前源码的分析结果，请稍后重试',409)
            return 'source_diagnostics_observed'
        deadline=time.monotonic()+self.spec.get('timeout_seconds',20)
        while time.monotonic()<deadline:
            reported=self.diagnostics.get(uri)
            if reported is not None and reported.get('version') in (None,1):
                return 'source_diagnostics_observed'
            if self.error:raise self.error
            self.changed.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.changed.wait(),max(.01,deadline-time.monotonic()))
        raise DevError('LSP_NOT_READY','语言服务尚未报告当前源码分析完成；没有把冷索引的空结果当作查无引用',409)

    async def drain_stderr(self):
        while await self.process.stderr.read(65536):pass

    async def close(self):
        if self.process:
            with contextlib.suppress(Exception):
                if self.process.returncode is None and self.reader and not self.reader.done():
                    await asyncio.wait_for(self.request('shutdown',None),1)
                    await self.notify('exit',{})
                    # Give the protocol peer a bounded chance to flush state and
                    # exit normally. Group cleanup below still reaps descendants.
                    await asyncio.wait_for(self.process.wait(),1)
            await self.agent.kill_process(self.process,include_finished=True)
        for t in (self.reader,self.stderr):
            if t:t.cancel()
        await asyncio.gather(*(t for t in (self.reader,self.stderr) if t),return_exceptions=True)
        if self.agent.processes.get(self.identifier) is self.process:self.agent.processes.pop(self.identifier,None)


class Projector:
    def __init__(self,engine,root,limit):self.engine,self.root,self.limit=engine,root,limit;self.count=0;self.omitted=0;self.cache={}
    def path(self,uri):
        try:
            p=urlsplit(uri)
            if p.scheme!='file' or p.netloc not in {'','localhost'} or p.query or p.fragment:raise ValueError()
            text=unquote(p.path)
            if os.name=='nt' and len(text)>2 and text[0]=='/' and text[2]==':':text=text[1:]
            rel=Path(text).relative_to(self.root).as_posix()
            actual=self.engine.path(self.root,rel,False)
            if not actual.is_file():raise ValueError()
            return rel
        except (ValueError,TypeError,DevError):self.omitted+=1;return None
    def source(self,path):
        if path not in self.cache:self.cache[path]=self.engine.text(self.engine.read_bytes(self.engine.path(self.root,path,False))).split('\n')
        return self.cache[path]
    def position(self,path,p):
        if not isinstance(p,dict) or type(p.get('line')) is not int or type(p.get('character')) is not int:raise ValueError()
        lines=self.source(path);line=p['line'];units=p['character']
        if not 0<=line<len(lines) or units<0:raise ValueError()
        text=lines[line];offset=0;column=0
        for char in text:
            if offset==units:break
            offset+=len(char.encode('utf-16-le'))//2;column+=1
            if offset>units:raise ValueError()
        if offset!=units:raise ValueError()
        return {'line':line+1,'column':column+1}
    def range(self,path,value):
        start=self.position(path,value['start']);end=self.position(path,value['end'])
        if (end['line'],end['column']) < (start['line'],start['column']):raise ValueError()
        return {'start':start,'end':end}
    def location(self,item):
        if not isinstance(item,dict):self.omitted+=1;return None
        path=self.path(item.get('uri') or item.get('targetUri') or '')
        if path is None:return None
        try:return {'path':path,'range':self.range(path,item.get('targetSelectionRange') or item.get('range'))}
        except (ValueError,KeyError,TypeError,DevError):self.omitted+=1;return None
    def take(self,item):
        if self.count>=self.limit:self.omitted+=1;return False
        self.count+=1;return True
    def symbols(self,items,path=None,depth=0):
        if items is None:return []
        if not isinstance(items,list):raise DevError('LSP_PROTOCOL','符号响应必须是数组')
        if depth>24:self.omitted+=len(items);return []
        result=[]
        for item in items:
            if not isinstance(item,dict):self.omitted+=1;continue
            loc=self.location(item.get('location',{})) if 'location' in item else None
            if loc is None and path and 'location' not in item:
                try:loc={'path':path,'range':self.range(path,item.get('selectionRange') or item['range'])}
                except (ValueError,KeyError,TypeError,DevError):self.omitted+=1;continue
            if loc and self.take(item):
                row={**loc,'name':str(item.get('name',''))[:300],'kind':item.get('kind'),'detail':str(item.get('detail',''))[:1000]}
                if item.get('children'):row['children']=self.symbols(item['children'],loc['path'],depth+1)
                result.append(row)
        return result


async def query(agent,identifier,project,args):
    root,spec=agent.engine.root(project)
    if not project.get('allow_tasks') or not spec.get('allow_tasks'):raise DevError('TASKS_DISABLED','语义查询需本机与项目执行许可',403)
    language=args['language'] or LANGUAGES.get(Path(args['path']).suffix.lower())
    config=selected(agent.config,project,language)
    if not config:raise DevError('LSP_NOT_CONFIGURED','此项目未配置并授权对应语言服务；没有自动安装或改用文本匹配',409)
    source=None;sha=None;uri=None;position=None
    if args['action']=='workspace_symbols' and not args['path']:
        # A file-scoped protocol barrier also works when a workspace query had
        # no path. Discover a bounded allowed seed, never a private config file.
        from agent.coding_reviews import excluded
        import stat
        for candidate,relative,info in agent.engine.walk(root,root,exclude=excluded,deadline=time.monotonic()+2):
            if stat.S_ISREG(info.st_mode) and LANGUAGES.get(candidate.suffix.lower())==language:
                args={**args,'path':relative,'line':1,'column':1};break
        if not args['path']:
            raise DevError('LSP_WORKSPACE_SEED','未找到可分析的授权源码，请提供本语言的项目相对路径作为初始化依据',409)
    if args['path']:
        path=agent.engine.path(root,args['path'],False);raw=agent.engine.read_bytes(path);source=agent.engine.text(raw)
        sha=hashlib.sha256(raw).hexdigest();uri=path.as_uri()
        if args['expected_sha256'] and sha!=args['expected_sha256']:raise DevError('LSP_SOURCE_CHANGED','源码 SHA 已变化，请重新读取后定位',409)
        lines=source.split('\n');line=args['line']-1;column=args['column']-1
        if not 0<=line<len(lines) or not 0<=column<=len(lines[line]):raise DevError('LSP_POSITION','查询位置超出源码')
        position={'line':line,'character':len(lines[line][:column].encode('utf-16-le'))//2}
    peer=Peer(agent,identifier,project,root,config);projector=Projector(agent.engine,root,args['limit']);action=args['action'];fresh=True
    try:
        async with asyncio.timeout(min(config.get('timeout_seconds',20)*3,120)):
            capabilities=await peer.start()
            if uri:await peer.notify('textDocument/didOpen',{'textDocument':{'uri':uri,'languageId':language,'version':1,'text':source}})
            params={'textDocument':{'uri':uri},'position':position};items=[];extra={}
            if action in {'workspace_symbols','references','incoming_calls','outgoing_calls'}:
                extra={'index_state':await peer.await_source_diagnostics(uri,capabilities),'index_seed':args['path'],
                       'workspace_completeness':'language-server reported; not a proof of complete repository coverage'}
            if action=='workspace_symbols':items=projector.symbols(await peer.request('workspace/symbol',{'query':args['query']}))
            elif action=='symbols':items=projector.symbols(await peer.request('textDocument/documentSymbol',{'textDocument':{'uri':uri}}),args['path'])
            elif action in {'definition','references'}:
                if action=='references':params['context']={'includeDeclaration':True}
                result=await peer.request('textDocument/'+action,params)
                if result is None:result=[]
                if isinstance(result,dict):result=[result]
                if not isinstance(result,list):raise DevError('LSP_PROTOCOL','位置响应格式无效')
                seen=set()
                for item in result:
                    loc=projector.location(item)
                    if loc:
                        key=json.dumps(loc,sort_keys=True)
                        if key not in seen and projector.take(loc):items.append(loc);seen.add(key)
            elif action=='hover':
                result=await peer.request('textDocument/hover',params)
                content=result.get('contents',[]) if isinstance(result,dict) else []
                if not isinstance(content,list):content=[content]
                text='\n'.join(str(c.get('value','')) if isinstance(c,dict) else str(c) for c in content)
                extra={'text':text[:16000],'text_truncated':len(text)>16000}
            elif action=='diagnostics':
                if capabilities.get('diagnosticProvider'):
                    response=await peer.request('textDocument/diagnostic',{'textDocument':{'uri':uri}})
                    rawitems=response.get('items',[]) if isinstance(response,dict) else []
                    fresh=isinstance(response,dict) and response.get('kind')=='full'
                else:
                    deadline=time.monotonic()+min(config.get('timeout_seconds',20),12)
                    while uri not in peer.diagnostics and not peer.error and time.monotonic()<deadline:
                        peer.changed.clear()
                        with contextlib.suppress(asyncio.TimeoutError):await asyncio.wait_for(peer.changed.wait(),max(.01,deadline-time.monotonic()))
                    reported=peer.diagnostics.get(uri)
                    fresh=bool(reported is not None and reported.get('version') in (None,1))
                    rawitems=reported.get('diagnostics',[]) if reported else []
                for item in rawitems:
                    try:
                        row={'path':args['path'],'range':projector.range(args['path'],item['range']),
                             'message':str(item.get('message',''))[:2000],'severity':item.get('severity'),'code':str(item.get('code',''))[:100]}
                        if projector.take(row):items.append(row)
                    except (TypeError,ValueError,KeyError,DevError):projector.omitted+=1
                extra={'diagnostics_fresh':fresh,'diagnostics_state':'observed' if fresh else 'not_received','not_a_test_run':True}
            else:
                prepared=await peer.request('textDocument/prepareCallHierarchy',params)
                if prepared is None:prepared=[]
                if not isinstance(prepared,list):raise DevError('LSP_PROTOCOL','调用层级响应无效')
                for item in prepared[:8]:
                    if not projector.location(item):continue
                    method='callHierarchy/incomingCalls' if action=='incoming_calls' else 'callHierarchy/outgoingCalls'
                    calls=await peer.request(method,{'item':item}) or []
                    if not isinstance(calls,list):raise DevError('LSP_PROTOCOL','调用层级数据格式错误')
                    for call in calls:
                        target=call.get('from' if action=='incoming_calls' else 'to',{})
                        loc=projector.location(target)
                        if loc and projector.take(loc):items.append({**loc,'name':str(target.get('name',''))[:300],'kind':target.get('kind')})
            if source is not None:
                current=agent.engine.read_bytes(agent.engine.path(root,args['path'],False))
                fresh=fresh and hashlib.sha256(current).hexdigest()==sha
            return {'action':action,'language':language,'backend':'lsp','precision':'semantic','items':items,
                    'source_sha256':sha,'source_current':fresh,'truncated':projector.omitted>0,'omitted':projector.omitted,
                    'column_unit':'one-based Unicode scalar',**extra}
    except asyncio.TimeoutError as exc:raise DevError('LSP_TIMEOUT','语义查询超过总期限，已回收语言服务',409) from exc
    finally:await peer.close()
