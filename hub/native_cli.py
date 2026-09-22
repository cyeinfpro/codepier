"""Admin-only native terminal protocol and private durable offline cache."""
from __future__ import annotations
import asyncio
import base64
from contextlib import closing
from collections import OrderedDict
import json
import time
import uuid
from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse
from shared.native_cli import database, public, identifier, LIVE, TOTAL_QUOTA
from shared.util import DevError


class NativeService:
    def __init__(self,runtime):
        self.runtime=runtime
        self.directory=runtime.store.directory/'native-cli'
        self.pending={}
        self.changed=asyncio.Condition()
        self.revision=0
        self.event_cache=OrderedDict()
        with closing(database(self.directory)): pass

    def project(self,value,principal):
        p=self.runtime.project(value,principal)
        if not p['device_enabled'] or p['mode']!='write' or not p['allow_tasks']:
            raise DevError('CLI_FORBIDDEN','节点须启用，项目须允许写入与执行',403)
        return p

    def session_project(self,sid,principal):
        identifier(sid)
        with closing(database(self.directory)) as db:
            r=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        if not r: raise DevError('CLI_NOT_FOUND','未找到会话',404)
        p=self.project(r['project_id'],principal)
        if p['device_id']!=r['device_id'] or p['root']!=r['root']:
            raise DevError('CLI_MAPPING_CHANGED','项目映射已改变；当前面板不能读取原会话',403)
        return p

    async def request(self,action,project,args):
        if getattr(self.runtime, "panel_maintenance", None):
            self.runtime.panel_maintenance.guard()
        con=self.runtime.connections.get(project['device_id'])
        if not con or not self.runtime.online(project['device_id']):
            raise DevError('CLI_OFFLINE','节点离线；已同步历史可读，控制待重新连接后重试',409)
        if getattr(con,'native_protocol',0)!=1:
            raise DevError('CLI_AGENT_UPDATE_REQUIRED','此节点 Agent 尚不支持原生会话，请先在节点管理更新 Agent',409)
        if (action.startswith('chat_') or action=='start' and args.get('mode')=='chat') and getattr(con,'native_chat_protocol',0) not in (1,2):
            raise DevError('CLI_CHAT_AGENT_UPDATE_REQUIRED','此节点 Agent 尚不支持结构化对话；请更新该节点 Agent 后重试',409)
        if (action in {'chat_catalog','chat_command','chat_queue','chat_cancel','chat_steer'} or action=='start' and args.get('mode')=='chat' and (args.get('model') or args.get('effort'))) and getattr(con,'native_chat_protocol',0)<2:
            raise DevError('CLI_CHAT_AGENT_UPDATE_REQUIRED','此节点仍是基础聊天版 Agent；模型目录和完整控制需要更新 Agent，旧会话仍保留',409)
        if len(self.pending)>=64: raise DevError('CLI_BUSY','终端控制通道繁忙，请稍后重试',429)
        request=uuid.uuid4().hex
        future=asyncio.get_running_loop().create_future()
        self.pending[request]=(con,future)
        try:
            await con.send({'type':'native_request','request_id':request,'action':action,'project':dict(project),'args':args})
            result=await asyncio.wait_for(future,15)
            if not result.get('ok'):
                error=result.get('error',{})
                raise DevError(error.get('code','CLI_ERROR'),error.get('message','原生控制失败'),409,
                               **{k:v for k,v in error.items() if k not in ('code','message')})
            return result['data']
        except (asyncio.TimeoutError,ConnectionError):
            if action=='chat_catalog':
                raise DevError('CLI_CATALOG_TIMEOUT','模型目录读取超时；请确认节点在线后重新加载，无需刷新页面。未创建会话或发送消息。',409)
            raise DevError('CLI_UNCERTAIN','控制回执尚未到达；使用同一会话/输入回执重试，不要重复输入',409)
        finally: self.pending.pop(request,None)

    async def receive(self,device,connection,data):
        if data['type']=='native_reply':
            pending=self.pending.get(data.get('request_id'))
            if pending and pending[0] is connection and not pending[1].done():
                pending[1].set_result(data.get('result',{}))
            return
        rows=data.get('sessions',[]); chunks=data.get('chunks',[])
        if not isinstance(rows,list) or len(rows)>1000 or not isinstance(chunks,list) or len(chunks)>16:
            return
        offsets={}; changed=False
        with closing(database(self.directory)) as db, db:
            for row in rows:
                try:
                    sid=identifier(row['id'])
                    p=self.runtime.store.one('SELECT * FROM projects WHERE id=? AND device_id=?',(row['project_id'],device))
                    if not p or p['root']!=row['root'] or p['mode']!='write' or not p['allow_tasks']: continue
                    if row['device_id']!=device or row['provider'] not in ('pi','codex') or row['status'] not in LIVE|{'exited','interrupted','quota_error','cleared','deleted'}: continue
                    if any(not isinstance(row[k],str) or len(row[k])>2048 for k in ('title','cwd','root','error')): continue
                    prior=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
                    if prior and (prior['device_id'],prior['project_id'],prior['root'])!=(device,p['id'],p['root']): continue
                    if prior:
                        # Session IDs are never restarted/reused: destructive states
                        # are permanent tombstones, even if a buffered old packet or
                        # the original HTTP start reply arrives after deletion.
                        if prior['status'] == 'deleted' and row['status'] != 'deleted':
                            continue
                        if prior['status'] == 'cleared' and row['status'] not in {'cleared','deleted'}:
                            continue
                        if prior['status'] not in LIVE and row['status'] in LIVE:
                            continue
                        if prior['status'] != 'starting' and row['status'] == 'starting':
                            continue
                        if row['status'] not in {'cleared','deleted'} and row['updated'] < prior['updated']:
                            continue
                    normalized=public(row)
                    if normalized.get('mode','terminal') not in {'chat','terminal'}: continue
                    if prior and prior['mode']!=normalized.get('mode','terminal'): continue
                    keys=list(normalized)
                    if not prior or any(prior[k]!=normalized[k] for k in keys):
                        changed=True
                        db.execute('INSERT INTO sessions('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+') ON CONFLICT(id) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in keys if k!='id'),[normalized[k] for k in keys])
                    if row['status'] in {'cleared','deleted'}:
                        db.execute('DELETE FROM output WHERE session=?',(sid,))
                    offsets[sid]=db.execute('SELECT COALESCE(MAX(offset+length(data)),0) FROM output WHERE session=?',(sid,)).fetchone()[0]
                except (KeyError,ValueError,TypeError): continue
            for chunk in chunks:
                sid=chunk.get('id')
                if sid not in offsets: continue
                state=db.execute('SELECT status FROM sessions WHERE id=?',(sid,)).fetchone()[0]
                if state in {'cleared','deleted'}: continue
                try:
                    raw=base64.b64decode(chunk['data'],validate=True)
                    offset=chunk['offset']
                    if type(offset) is not int or offset!=offsets[sid] or not 0<len(raw)<=65536: continue
                    # Cache quota is explicit; withhold ACK so the Agent retains and retries data.
                    used=db.execute('SELECT COALESCE(sum(length(data)),0) FROM output').fetchone()[0]
                    if used+len(raw)>TOTAL_QUOTA:
                        db.execute("UPDATE sessions SET error='HUB_CACHE_QUOTA: local Agent transcript retained; export/clear cache' WHERE id=?",(sid,));continue
                    db.execute('INSERT OR IGNORE INTO output VALUES (?,?,?)',(sid,offset,raw));offsets[sid]+=len(raw);changed=True
                except (ValueError,KeyError,TypeError): continue
        if changed:
            async with self.changed:
                self.revision+=1
                self.event_cache.clear()
                self.changed.notify_all()
        await connection.send({'type':'native_ack','offsets':offsets})

    @staticmethod
    def search_transcript(db,sid,query):
        """Search synchronized text, including UTF-8/JSON split across chunks.

        Only public conversation text participates; settings, credentials and
        approval payloads are not a searchable side channel.
        """
        buffer=bytearray();tail='';last_key=None;keep=max(0,len(query)-1)
        for row in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset',(sid,)):
            buffer.extend(row['data'])
            while b'\n' in buffer:
                line,_,rest=buffer.partition(b'\n');buffer[:]=rest
                try: event=json.loads(line)
                except (ValueError,UnicodeError): continue
                if not isinstance(event,dict) or event.get('type') not in {'user','message','delta','reasoning','tool','error','status'}:
                    tail='';last_key=None;continue
                text=event.get('text',event.get('delta',''))
                if not isinstance(text,str): continue
                key=(event.get('receipt'),event.get('item_id')) if event.get('type')=='delta' else None
                if key is None or key!=last_key: tail=''
                text=text.casefold()
                if query in tail+text: return True
                tail=(tail+text)[-keep:] if key is not None and keep else '';last_key=key
            if len(buffer)>1048576: buffer.clear();tail=''
        return False

    def event_batch(self,sid,cursor):
        """Bounded shared replay cache; cursors only advance over complete JSON lines."""
        key=(sid,cursor,self.revision)
        if key in self.event_cache:
            self.event_cache.move_to_end(key)
            return self.event_cache[key]
        with closing(database(self.directory)) as db:
            row=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
            if not row: raise DevError('CLI_NOT_FOUND','未找到会话',404)
            metadata=public(row)
            if metadata.get('mode','terminal')!='chat':
                raise DevError('CLI_TERMINAL_SESSION','此记录属于旧终端历史，不能作为聊天对话回放；原记录仍保留',409)
            if cursor:
                prev=db.execute('SELECT offset,data FROM output WHERE session=? AND offset<? AND offset+length(data)>=? ORDER BY offset DESC LIMIT 1',(sid,cursor,cursor)).fetchone()
                if not prev or prev['data'][cursor-prev['offset']-1:cursor-prev['offset']]!=b'\n':
                    raise DevError('CLI_OFFSET','事件游标不是已完成的事件边界',409)
            raw=b''
            for out in db.execute('SELECT offset,data FROM output WHERE session=? AND offset+length(data)>? ORDER BY offset',(sid,cursor)):
                raw+=out['data'][max(0,cursor-out['offset']):]
                if len(raw)>1048576: break
            lines=raw.split(b'\n');frames=[];next_cursor=cursor
            for line in lines[:-1]:
                next_cursor+=len(line)+1
                try:
                    item=json.loads(line)
                    if not isinstance(item,dict): raise ValueError('event must be an object')
                except (ValueError,UnicodeError):
                    raise DevError('CLI_CHAT_PROTOCOL','结构化事件损坏；请在节点查看原生会话',409)
                frames.append((next_cursor,item))
                if len(frames)>=128 or next_cursor-cursor>=262144: break
            if not frames and len(raw)>1048576:
                raise DevError('CLI_CHAT_PROTOCOL','结构化事件超过回放上限',409)
        result=(metadata,frames,next_cursor)
        self.event_cache[key]=result
        # Entries are bounded independently from the private durable spool.
        while len(self.event_cache)>32: self.event_cache.popitem(last=False)
        return result


def make_native_router(auth,runtime):
    router=APIRouter(prefix='/api/native')
    service=runtime.native

    @router.get('/sessions')
    async def sessions(request:Request,project:str='',provider:str='',status:str='',q:str='',offset:int=0,limit:int=40,mode:str=''):
        principal=auth.admin(request)
        offset=max(0,offset);limit=max(1,min(limit,100));query=q[:200].strip().casefold()
        result=[];projects={}
        with closing(database(service.directory)) as db:
            # Revalidate each mapping once per request, before filtering, counting
            # or returning any metadata. No cross-request authorization cache.
            for row in db.execute("SELECT * FROM sessions WHERE status!='deleted' ORDER BY updated DESC,created DESC,id DESC"):
                pid=row['project_id']
                if pid not in projects:
                    try: projects[pid]=dict(service.project(pid,principal))
                    except DevError: projects[pid]=None
                p=projects[pid]
                if not p or p['device_id']!=row['device_id'] or p['root']!=row['root']: continue
                if project and p['id']!=project or provider and row['provider']!=provider or status and row['status']!=status: continue
                if mode and row['mode']!=mode: continue
                alias=p.get('alias','');node=p.get('device_name','')
                metadata=' '.join((row['title'],alias,node,row['cwd'],row['root'],row['provider'])).casefold()
                if query and query not in metadata:
                    if not service.search_transcript(db,row['id'],query): continue
                result.append({**public(row),'online':runtime.online(p['device_id']),
                               'project_alias':alias,'device_name':node})
            total=len(result)
            # Removing the final row on a page returns the preceding valid page,
            # including archives beyond the former 1,000-row offset ceiling.
            offset=min(offset,((total-1)//limit)*limit) if total else 0
            page=result[offset:offset+limit]
            for row in page:
                row['cached_bytes']=db.execute('SELECT COALESCE(MAX(offset+length(data)),0) FROM output WHERE session=?',(row['id'],)).fetchone()[0]
        return {'sessions':page,'total':total,'offset':offset,'limit':limit}

    @router.get('/sessions/{sid}/output')
    async def output(sid:str,request:Request,offset:int=0):
        principal=auth.admin(request)
        service.session_project(sid,principal)
        if not 0<=offset<=2**40: raise DevError('CLI_OFFSET','无效回放偏移')
        with closing(database(service.directory)) as db:
            row=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
            reset=row['status'] in {'cleared','deleted'} and offset>0
            if reset: offset=0
            chunks=[];next_offset=offset
            for out in db.execute('SELECT * FROM output WHERE session=? AND offset+length(data)>? ORDER BY offset LIMIT 4',(sid,offset)):
                raw=out['data'][max(0,offset-out['offset']):]
                chunks.append(base64.b64encode(raw).decode('ascii'));next_offset=out['offset']+len(out['data'])
            return {'session':public(row),'offset':offset,'next':next_offset,'chunks':chunks,'reset':reset,
                    'online':runtime.online(row['device_id'])}

    @router.get('/sessions/{sid}/export')
    async def export(sid:str,request:Request,format:str='md'):
        principal=auth.admin(request)
        service.session_project(sid,principal)
        if format not in ('md','json'): raise DevError('CLI_FORMAT','Export format must be md or json')
        with closing(database(service.directory)) as db:
            row=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
            if row['mode']!='chat': raise DevError('CLI_TERMINAL_SESSION','Terminal output is not chat',409)
            boundary=db.execute('SELECT COALESCE(MAX(offset+length(data)),0) FROM output WHERE session=?',(sid,)).fetchone()[0]
        async def content():
            cursor=0; buffer=bytearray(); first=True; messages=OrderedDict(); message_bytes=0; serial=0
            yield '[' if format=='json' else '# Chat export\n\n'
            while cursor<boundary:
                service.session_project(sid,auth.admin(request))
                with closing(database(service.directory)) as db:
                    current=db.execute('SELECT status FROM sessions WHERE id=?',(sid,)).fetchone()
                    if not current or current['status'] in ('cleared','deleted'): break
                    rows=db.execute('SELECT offset,data FROM output WHERE session=? AND offset+length(data)>? AND offset<? ORDER BY offset LIMIT 4',(sid,cursor,boundary)).fetchall()
                if not rows: break
                for row in rows:
                    if row['offset']>cursor: raise DevError('CLI_EXPORT_GAP','Persisted chat has a sync gap',409)
                    raw=row['data'][max(0,cursor-row['offset']):max(0,boundary-row['offset'])]
                    cursor+=len(raw);buffer.extend(raw)
                    while b'\n' in buffer:
                        line,_,rest=buffer.partition(b'\n');buffer[:]=rest
                        event=json.loads(line)
                        # Text-only export: native RPC details/config/approval payloads
                        # and attachment filesystem paths are deliberately excluded.
                        safe={k:v for k,v in event.items() if k in ('type','receipt','parent_receipt','text','name','status','state','item_id','tool_id','steering') and isinstance(v,(str,bool,int,float,type(None)))}
                        if format=='json':
                            yield ('' if first else ',')+json.dumps(safe,ensure_ascii=False)
                            first=False
                        elif isinstance(safe.get('text'),str):
                            kind=safe.get('type','event');item=safe.get('item_id');serial+=1
                            key=(safe.get('receipt'),item or 'assistant','message') if kind in {'delta','message'} else (
                                (safe.get('receipt'),item,kind) if item else ('event',serial,kind))
                            previous=messages.get(key,{'type':'message' if kind=='delta' else kind,'text':''})
                            text=previous['text']+safe['text'] if kind=='delta' else safe['text']
                            message_bytes+=len(text.encode('utf-8'))-len(previous['text'].encode('utf-8'))
                            if message_bytes>TOTAL_QUOTA:raise DevError('CLI_EXPORT_LIMIT','对话导出超过存储预算')
                            messages[key]={'type':previous['type'],'text':text}
                        if format=='md' and safe.get('type')=='done':
                            for message in messages.values():
                                yield message['type']+'\n\n'+message['text']+'\n\n'
                            messages.clear();message_bytes=0
                    if len(buffer)>1048576: raise DevError('CLI_CHAT_PROTOCOL','Export event exceeds limit',409)
                await asyncio.sleep(0)
            if format=='json': yield ']'
            else:
                service.session_project(sid,auth.admin(request))
                for message in messages.values():
                    yield message['type']+'\n\n'+message['text']+'\n\n'
        return StreamingResponse(content(),media_type='application/json' if format=='json' else 'text/plain',headers={
            'Content-Disposition':f'attachment; filename="chat-{sid}.{format}"',
            'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Chat-Export-Boundary':str(boundary)})

    @router.get('/sessions/{sid}/events')
    async def events(sid:str,request:Request,cursor:int=0):
        principal=auth.admin(request)
        service.session_project(sid,principal)
        try:
            cursor=int(request.headers.get('last-event-id',cursor))
        except (TypeError,ValueError): raise DevError('CLI_OFFSET','无效事件游标')
        if not 0<=cursor<=2**40: raise DevError('CLI_OFFSET','无效事件游标')
        service.event_batch(sid,cursor)  # Fail before streaming response headers.
        async def stream():
            nonlocal cursor
            seen=-1;last_metadata=None
            while True:
                if await request.is_disconnected(): return
                try:
                    # Cookies can expire, sessions can be revoked and mappings can
                    # change while a response is open. Recheck on every wakeup.
                    service.session_project(sid,auth.admin(request))
                    revision=service.revision
                    if seen!=revision:
                        metadata,frames,next_cursor=service.event_batch(sid,cursor)
                        if metadata!=last_metadata:
                            yield 'event: session\ndata: '+json.dumps(metadata,ensure_ascii=False)+'\n\n'
                            last_metadata=metadata
                        for offset,item in frames:
                            yield 'id: '+str(offset)+'\nevent: chat\ndata: '+json.dumps(item,ensure_ascii=False)+'\n\n'
                            cursor=offset
                        if frames:
                            # Drain bounded batches with backpressure; check access
                            # again before the next batch, including long replays.
                            await asyncio.sleep(0)
                            continue
                        seen=revision
                    keepalive=False
                    async with service.changed:
                        if service.revision!=seen: continue
                        try:
                            await asyncio.wait_for(service.changed.wait(),10)
                        except asyncio.TimeoutError:
                            keepalive=True
                    if keepalive: yield ': keepalive\n\n'
                except DevError as exc:
                    yield 'event: error\ndata: '+json.dumps({'code':exc.code,'message':str(exc)},ensure_ascii=False)+'\n\n'
                    return
        return StreamingResponse(stream(),media_type='text/event-stream',headers={
            'Cache-Control':'no-cache, no-store','X-Accel-Buffering':'no',
            'X-Content-Type-Options':'nosniff'})

    @router.post('/{action}')
    async def action(action:str,request:Request):
        principal=auth.admin(request,True)
        body=await request.json()
        if not isinstance(body,dict) or not isinstance(body.get('args',{}),dict):
            raise DevError('CLI_INVALID','参数必须是对象')
        if action not in {'start','discover','lease','detach','input','resize','stop','rename','clear','delete','receipt','upload_begin','upload_chunk','upload_finish','upload_delete','upload_list','upload_bind','chat_prompt','chat_interrupt','chat_answer','chat_settings','chat_catalog','chat_command','chat_queue','chat_cancel','chat_steer','chat_review'}:
            raise DevError('CLI_INVALID','未知终端动作')
        project=dict(service.project(body.get('project',''),principal))
        args=body.get('args',{})
        if action not in {'start','discover','chat_catalog','upload_begin','upload_chunk','upload_finish','upload_delete','upload_list'}:
            try: identifier(args.get('id'))
            except ValueError: raise DevError('CLI_INVALID','无效会话编号')
            p=service.session_project(args['id'],principal)
            if p['id']!=project['id']: raise DevError('CLI_MAPPING_CHANGED','会话不属于此项目',403)
        result=await service.request(action,project,args)
        # Recheck after await: an admin may have revoked/remapped while request was in flight.
        principal=auth.admin(request,True)
        current=service.project(project['id'],principal)
        if (current['root'],current['device_id'])!=(project['root'],project['device_id']):
            raise DevError('CLI_MAPPING_CHANGED','操作期间映射改变；请在原节点检查',403)
        if action=='start':
            # Cache metadata immediately even before the first periodic sync.
            class NoAck:
                async def send(self,body): pass
            await service.receive(project['device_id'],NoAck(),{'type':'native_sync','sessions':[result],'chunks':[]})
        if action in {'clear','delete'}:
            with closing(database(service.directory)) as db,db:
                db.execute('DELETE FROM output WHERE session=?',(args['id'],))
                db.execute('UPDATE sessions SET size=0,status=?,updated=? WHERE id=?',('deleted' if action=='delete' else 'cleared',time.time(),args['id']))
            async with service.changed:
                service.revision+=1
                service.event_cache.clear()
                service.changed.notify_all()
        if action in {'start','stop','rename','clear','delete','upload_begin','upload_delete','upload_bind'}:
            # No prompts, terminal output, argv, filenames or credentials enter
            # the regular audit journal. Native content stays in its private DB.
            runtime.store.audit(principal.actor, 'native.'+action, args.get('id',args.get('file','')),
                                detail={'project_id':project['id'],'device_id':project['device_id'],
                                        'receipt':args.get('receipt','')})
        return result
    return router
