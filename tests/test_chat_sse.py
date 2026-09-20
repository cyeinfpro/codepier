"""Deterministic durable structured replay, capability and authorization tests."""
import asyncio
import base64
from contextlib import closing
import json
from types import SimpleNamespace
import uuid

import pytest
from starlette.requests import Request
from hub.native_cli import NativeService, make_native_router
from shared.native_cli import database, public
from shared.util import DevError


@pytest.fixture
def service(tmp_path):
    project={'id':'project','device_id':'device','root':str(tmp_path),'mode':'write','allow_tasks':True,'device_enabled':True}
    def get_project(value, principal):
        if value!=project['id'] or not project['allow_tasks']:
            raise DevError('FORBIDDEN','revoked',403)
        return project
    runtime=SimpleNamespace(store=SimpleNamespace(directory=tmp_path,one=lambda *a:project),project=get_project,connections={},online=lambda _:True)
    obj=NativeService(runtime);runtime.native=obj
    sid=uuid.uuid4().hex
    with closing(database(obj.directory)) as db,db:
        db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(sid,'project','device',str(tmp_path),str(tmp_path),'pi','Chat','running',1,1,'chat'))
    return obj,project,sid


async def sync(obj,sid,raw,offset=0):
    with closing(database(obj.directory)) as db:
        row=public(db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())
    sent=[]
    async def send(data):sent.append(data)
    await obj.receive('device',SimpleNamespace(send=send),{'type':'native_sync','sessions':[row],'chunks':[{'id':sid,'offset':offset,'data':base64.b64encode(raw).decode()}]})
    return sent[-1]['offsets'][sid]


@pytest.mark.asyncio
async def test_split_utf8_json_lines_replay_and_duplicate_ack(service):
    obj,_,sid=service
    raw=(json.dumps({'type':'text_delta','text':'你好'},ensure_ascii=False)+'\n').encode()
    split=raw.index('你'.encode())+1
    assert await sync(obj,sid,raw[:split])==split
    assert obj.event_batch(sid,0)[1]==[]
    assert await sync(obj,sid,raw[split:],split)==len(raw)
    frames=obj.event_batch(sid,0)[1]
    assert frames==[(len(raw),{'type':'text_delta','text':'你好'})]
    assert await sync(obj,sid,raw[split:],split)==len(raw)
    assert obj.event_batch(sid,len(raw))[1]==[]
    with pytest.raises(DevError,match='边界'):obj.event_batch(sid,split)


@pytest.mark.asyncio
async def test_old_metadata_is_terminal_and_unchanged_sync_not_notified(service):
    obj,_,sid=service
    with closing(database(obj.directory)) as db:
        row=public(db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())
    async def send(data):pass
    await obj.receive('device',SimpleNamespace(send=send),{'sessions':[row],'chunks':[],'type':'native_sync'})
    assert obj.revision==0
    old=dict(row,id=uuid.uuid4().hex)
    for key in ('mode','native_thread','chat_settings'):old.pop(key,None)
    await obj.receive('device',SimpleNamespace(send=send),{'sessions':[old],'chunks':[],'type':'native_sync'})
    with closing(database(obj.directory)) as db:
        assert db.execute('SELECT mode FROM sessions WHERE id=?',(old['id'],)).fetchone()[0]=='terminal'
    with pytest.raises(DevError,match='旧终端历史') as caught:
        obj.event_batch(old['id'],0)
    assert caught.value.code == 'CLI_TERMINAL_SESSION'
    assert caught.value.status == 409


@pytest.mark.asyncio
async def test_older_agent_rejects_chat_but_preserves_native_protocol(service):
    obj,project,_=service
    obj.runtime.connections['device']=SimpleNamespace(native_protocol=1)
    for action,args in [('start',{'mode':'chat'}),('chat_prompt',{}),('chat_interrupt',{}),('chat_answer',{})]:
        with pytest.raises(DevError,match='尚不支持结构化对话'):
            await obj.request(action,project,args)


class TestAuth:
    allowed=True
    def admin(self,request,write=False):
        if not self.allowed:raise DevError('LOGIN_REQUIRED','revoked',401)
        if write and request.headers.get('x-rd-csrf')!='valid':raise DevError('CSRF_REJECTED','csrf',403)
        return SimpleNamespace(actor='test')


def request(headers=(),body=b'{}'):
    async def receive():return {'type':'http.request','body':body,'more_body':False}
    return Request({'type':'http','method':'GET','path':'/','headers':list(headers),'query_string':b''},receive)


@pytest.mark.asyncio
async def test_stream_last_id_auth_revocation_and_action_csrf(service):
    obj,project,sid=service;auth=TestAuth()
    first=b'{"type":"text_delta","text":"one"}\n';second=b'{"type":"text_delta","text":"two"}\n'
    await sync(obj,sid,first+second)
    routes=make_native_router(auth,obj.runtime).routes
    endpoint=next(r.endpoint for r in routes if r.path.endswith('/events'))
    req=request([(b'last-event-id',str(len(first)).encode())])
    async def connected():return False
    req.is_disconnected=connected
    response=await endpoint(sid,req,0)
    assert response.headers['x-accel-buffering']=='no'
    iterator=response.body_iterator
    assert 'event: session' in await anext(iterator)
    frame=await anext(iterator)
    assert 'two' in frame and 'one' not in frame
    assert 'id: '+str(len(first+second)) in frame
    auth.allowed=False
    assert 'LOGIN_REQUIRED' in await anext(iterator)
    with pytest.raises(StopAsyncIteration):await anext(iterator)
    with pytest.raises(DevError):await endpoint(sid,req,0)
    auth.allowed=True
    action=next(r.endpoint for r in routes if r.path.endswith('/{action}'))
    with pytest.raises(DevError,match='csrf'):
        await action('chat_prompt',request())
    project['allow_tasks']=False
    with pytest.raises(DevError,match='revoked'):await endpoint(sid,req,0)
