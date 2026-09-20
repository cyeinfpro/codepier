"""Scoped artifact metadata and backpressured, authenticated HTTP downloads."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import math
import re
import time
import uuid
from urllib.parse import quote
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from hub.workflows import encode_cursor, decode_cursor
from shared.util import DevError

CHUNK=256*1024
MAX_BYTES=512*1024*1024


def byte_range(value, size):
    if not value:
        return 0,size-1,False
    match=re.fullmatch(r'bytes=(\d*)-(\d*)',value)
    if not match or not any(match.groups()) or size==0 or len(value)>100:
        raise DevError('INVALID_RANGE','只支持单个有效的 bytes 范围',416)
    left,right=match.groups()
    if left:
        start=int(left);end=min(int(right),size-1) if right else size-1
        if start>=size or start>end:
            raise DevError('INVALID_RANGE','下载范围超出文件',416)
    else:
        count=int(right)
        if count<=0:
            raise DevError('INVALID_RANGE','后缀范围必须大于零',416)
        start=max(0,size-count);end=size-1
    return start,end,True


class ArtifactService:
    def __init__(self,runtime):
        self.runtime=runtime
        self.pending={}
        self.active_downloads=0

    def register_result(self,op,data):
        """Called before the operation payload is cleared; failed DB writes replay safely."""
        identifier=data.get('artifact_id');name=data.get('name');size=data.get('bytes');sha=data.get('sha256')
        created,expires=data.get('created'),data.get('expires')
        if (identifier!=op['id'] or not isinstance(name,str) or not 1<=len(name)<=180 or name in {'.','..'} or
                any(ord(c)<32 or ord(c)==127 or c in '/\\' for c in name) or
                type(size) is not int or not 0<=size<=MAX_BYTES or not isinstance(sha,str) or not re.fullmatch(r'[a-f0-9]{64}',sha) or
                type(created) not in {int,float} or type(expires) not in {int,float} or
                not math.isfinite(created) or not math.isfinite(expires) or not created<expires<=created+7*86400+1):
            raise DevError('INVALID_ARTIFACT_RESULT','设备返回的产物信息无效，请核对本机文件')
        old=self.runtime.store.one('SELECT * FROM artifacts WHERE id=?',(identifier,))
        if old:
            if old['sha256']!=sha or old['bytes']!=size or old['project_id']!=op['project_id']:
                raise DevError('ARTIFACT_CONFLICT','产物编号对应的已保存内容不同')
            return
        payload=json.loads(self.runtime.store.decrypt(op['payload']))
        self.runtime.store.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (identifier,op['project_id'],op['device_id'],op['grant_id'],op['actor'],payload['project']['root'],
             name,size,sha,created,expires,payload['args'].get('source_operation_id','')))

    def row(self,identifier,principal):
        row=self.runtime.store.one('SELECT * FROM artifacts WHERE id=?',(identifier,))
        if not row or (not principal.admin and (principal.grant_id is None or row['grant_id']!=principal.grant_id)):
            raise DevError('ARTIFACT_NOT_FOUND','找不到当前授权范围的产物',404)
        project=self.runtime.project(row['project_id'],principal)
        if project['root']!=row['root'] or project['device_id']!=row['device_id']:
            raise DevError('ARTIFACT_MAPPING_CHANGED','项目映射已经变化，旧产物不能在新映射下下载',409)
        return row,project

    def public(self,row):
        return {k:row[k] for k in ('name','bytes','sha256','created','expires','project_id','source_operation_id')}|{
            'artifact_id':row['id'],'download_path':'/api/artifacts/'+row['id']+'/download',
            'expired':row['expires']<=time.time(),'device_online':self.runtime.online(row['device_id']),
            'authentication':'Panel session or current read-scoped Bearer token; no anonymous URL',
            'immutable':True,'range_supported':True}

    def get(self,args,principal):
        return self.public(self.row(args['artifact_id'],principal)[0])

    def list(self,args,principal):
        clauses=[];values=[]
        if not principal.admin:
            clauses.append('a.grant_id=?');values.append(principal.grant_id)
            if '*' not in principal.projects:
                clauses.append('a.project_id IN (%s)'%(','.join('?' for _ in principal.projects) or 'NULL'))
                values.extend(principal.projects)
        if args['project']:
            project=self.runtime.project(args['project'],principal)
            clauses.append('a.project_id=?');values.append(project['id'])
        if args['cursor']:
            created,identifier=decode_cursor(args['cursor'])
            clauses.append('(a.created<? OR (a.created=? AND a.id<?))');values.extend((created,created,identifier))
        rows=self.runtime.store.all('SELECT a.* FROM artifacts a JOIN projects p ON p.id=a.project_id AND p.root=a.root AND p.device_id=a.device_id'+
            (' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY a.created DESC,a.id DESC LIMIT ?',(*values,args['limit']+1))
        more=len(rows)>args['limit'];rows=rows[:args['limit']]
        return {'artifacts':[self.public(row) for row in rows],
                'next_cursor':encode_cursor(rows[-1]['created'],rows[-1]['id']) if more and rows else None}

    async def chunk(self,row,project,offset):
        runtime=self.runtime;connection=runtime.connections.get(row['device_id'])
        if not connection or not runtime.online(row['device_id']):
            raise DevError('ARTIFACT_DEVICE_OFFLINE','产物保存在本机；设备离线，恢复连接后可从原位置续传',503)
        if len(self.pending)>=4:
            raise DevError('ARTIFACT_BUSY','同时传输的产物过多，请稍后继续',429)
        identifier=uuid.uuid4().hex;future=asyncio.get_running_loop().create_future()
        self.pending[identifier]=(connection,future)
        try:
            await connection.send({'type':'artifact_read','request_id':identifier,'artifact_id':row['id'],'offset':offset,
                                   'project':{k:project[k] for k in ('id','alias','root','mode','allow_tasks')}})
            try:
                result=await asyncio.wait_for(future,12)
            except asyncio.TimeoutError as exc:
                raise DevError('ARTIFACT_TRANSFER_TIMEOUT','产物分段等待超时；使用原产物和 Range 续传，不要重跑生成命令',503) from exc
            if not isinstance(result,dict) or result.get('ok') is not True:
                error=result.get('error',{}) if isinstance(result,dict) else {}
                raise DevError(error.get('code','ARTIFACT_TRANSFER_ERROR'),error.get('message','产物传输失败'),503)
            data=result.get('data',{})
            encoded=data.get('data') if isinstance(data,dict) else None
            if not isinstance(encoded,str) or len(encoded)>CHUNK*2:
                raise DevError('ARTIFACT_CORRUPT','产物分段格式无效',502)
            try:
                binary=base64.b64decode(encoded,validate=True)
            except (ValueError,TypeError) as exc:
                raise DevError('ARTIFACT_CORRUPT','产物分段编码无效',502) from exc
            if (data.get('artifact_id')!=row['id'] or data.get('offset')!=offset or data.get('sha256')!=row['sha256'] or
                    len(binary)!=min(CHUNK,row['bytes']-offset) or hashlib.sha256(binary).hexdigest()!=data.get('chunk_sha256')):
                raise DevError('ARTIFACT_CORRUPT','产物分段校验失败，传输已停止',502)
            return binary
        except (OSError,ConnectionError) as exc:
            raise DevError('ARTIFACT_DEVICE_OFFLINE','连接暂时不可用，请按原文件位置续传',503) from exc
        finally:
            self.pending.pop(identifier,None)
            if not future.done():future.cancel()

    def receive(self,connection,data):
        identifier=data.get('request_id')
        if not isinstance(identifier,str):return
        item=self.pending.get(identifier)
        if item and item[0] is connection and not item[1].done():
            item[1].set_result(data.get('result'))

    def close(self):
        for _,future in self.pending.values():
            if not future.done():future.cancel()
        self.pending.clear()


def make_artifact_router(auth,runtime):
    router=APIRouter();service=runtime.artifacts
    def principal(request):
        owner=auth.bearer(request) if request.headers.get('authorization') else auth.admin(request)
        if 'read' not in owner.scopes:
            raise DevError('INSUFFICIENT_SCOPE','下载需要读取权限',403)
        if not request.headers.get('authorization'):auth.check_origin(request)
        return owner

    @router.get('/api/artifacts/{identifier}')
    async def metadata(identifier:str,request:Request):
        return service.get({'artifact_id':identifier},principal(request))

    @router.api_route('/api/artifacts/{identifier}/download',methods=['GET','HEAD'])
    async def download(identifier:str,request:Request):
        owner=principal(request);row,project=service.row(identifier,owner)
        if row['expires']<=time.time():raise DevError('ARTIFACT_EXPIRED','产物快照已过期，请重新登记',410)
        etag='"'+row['sha256']+'"'
        requested=request.headers.get('range')
        if request.headers.get('if-range') and request.headers['if-range']!=etag:requested=None
        try:
            start,end,partial=byte_range(requested,row['bytes'])
        except DevError:
            return Response(status_code=416,headers={'Content-Range':f"bytes */{row['bytes']}",'Accept-Ranges':'bytes','ETag':etag})
        headers={'Content-Length':str(max(0,end-start+1)),'Accept-Ranges':'bytes','ETag':etag,
                 'Content-Disposition':"attachment; filename=\"artifact\"; filename*=UTF-8''"+quote(row['name'],safe=''),
                 'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'}
        if partial:headers['Content-Range']=f"bytes {start}-{end}/{row['bytes']}"
        status=206 if partial else 200
        if request.method=='HEAD' or row['bytes']==0:
            return Response(status_code=status,headers=headers,media_type='application/octet-stream')
        if service.active_downloads>=4:raise DevError('ARTIFACT_BUSY','最多同时下载四个产物',429)
        service.active_downloads+=1
        released=False
        def release():
            nonlocal released
            if not released:
                released=True;service.active_downloads-=1
        try:
            offset=(start//CHUNK)*CHUNK
            first=await service.chunk(row,project,offset)
            runtime.store.audit(owner.actor,'artifact.download',identifier,detail={'start':start,'end':end,'bytes':row['bytes']})
        except BaseException:
            release();raise
        async def stream():
            current=offset;binary=first
            try:
                while current<=end:
                    if await request.is_disconnected():return
                    current_owner=principal(request)
                    current_row,current_project=service.row(identifier,current_owner)
                    if current_row['expires']<=time.time():raise DevError('ARTIFACT_EXPIRED','产物已过期',410)
                    if current!=offset:binary=await service.chunk(current_row,current_project,current)
                    yield binary[max(0,start-current):min(len(binary),end-current+1)]
                    current+=CHUNK
            finally:
                release()
        class DownloadResponse(StreamingResponse):
            async def __call__(self,scope,receive,send):
                try:await super().__call__(scope,receive,send)
                finally:release()
        return DownloadResponse(stream(),status_code=status,headers=headers,media_type='application/octet-stream')
    return router
