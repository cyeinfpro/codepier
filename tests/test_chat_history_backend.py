"""Global history: authorization before counts/search, stable ordering and paging."""
import asyncio
import json
import threading
import uuid
from contextlib import closing
from types import SimpleNamespace
import pytest
from tests.test_chat_sse import service, TestAuth, request
from hub.native_cli import make_native_router
from shared.native_cli import database
from shared.util import DevError


def endpoint(obj):
    return next(r.endpoint for r in make_native_router(TestAuth(),obj.runtime).routes if r.path=='/api/native/sessions')


def add(obj,project,**extra):
    row=dict(id=uuid.uuid4().hex,project_id=project['id'],device_id=project['device_id'],root=project['root'],cwd=project['root'],provider='pi',title='Conversation',status='exited',created=1,updated=1,mode='chat')
    row.update(extra)
    store=obj.runtime.store
    if not store.one('SELECT id FROM projects WHERE id=?',(project['id'],)):
        store.execute('INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created,owner_user_id) VALUES(?,?,?,?,?,?,?,1,?)',
                      (project['id'],project['id'],project['id'],project['device_id'],project['root'],project['mode'],int(project['allow_tasks']),'owner'))
    with closing(database(obj.directory)) as db,db:
        db.execute('INSERT INTO sessions('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()))
    return row['id']


@pytest.mark.asyncio
async def test_listing_batches_ownership_transaction(service, monkeypatch):
    from contextlib import contextmanager
    obj, project, _ = service
    for _ in range(20):
        add(obj, project)
    calls = []
    transaction = obj.runtime.store.transaction

    @contextmanager
    def counted(*args, **kwargs):
        calls.append(1)
        with transaction(*args, **kwargs):
            yield

    monkeypatch.setattr(obj.runtime.store, 'transaction', counted)
    data = await endpoint(obj)(request())
    assert data['total'] == 21
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_transcript_search_has_shared_byte_budget(service, monkeypatch):
    import hub.native_cli as module
    obj, project, sid = service
    other = add(obj, project)
    raw = b'{"type":"message","text":"no match"}\n'
    with closing(database(obj.directory)) as db, db:
        for identifier in (sid, other):
            db.execute('INSERT INTO output VALUES (?,?,?)', (identifier, 0, raw))
    monkeypatch.setattr(module, 'SEARCH_BYTES', len(raw) + 1)
    with pytest.raises(DevError) as error:
        await endpoint(obj)(request(), q='missing-search-text')
    assert error.value.code == 'CLI_SEARCH_LIMIT'
    assert not obj.searching


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_search_does_not_block_writer_rechecks_access_and_drains_cancel(service, monkeypatch, cancel):
    obj, project, _ = service
    entered, release = threading.Event(), threading.Event()
    worker = await obj.runtime.store.run(threading.get_ident)

    def search(*args, **kwargs):
        assert threading.get_ident() != worker
        entered.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(obj, 'search_transcript', search)
    task = asyncio.create_task(endpoint(obj)(request(), q='needle'))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert await asyncio.wait_for(obj.runtime.store.run(lambda: 'writer remains available'), 1) == 'writer remains available'
        with pytest.raises(DevError) as busy:
            await endpoint(obj)(request(), q='needle')
        assert busy.value.code == 'CLI_SEARCH_BUSY'
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and obj.searching
        else:
            project['allow_tasks'] = False
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)['total'] == 0
        assert not obj.searching
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
async def test_markdown_export_tail_auth_runs_off_loop_and_rechecks_each_message(service):
    obj, _, sid = service
    loop_thread = threading.get_ident()
    auth = TestAuth()
    panel = auth.panel

    def checked(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        return panel(*args, **kwargs)

    auth.panel = checked
    raw = b'{"type":"user","text":"first"}\n{"type":"message","text":"second"}\n'
    with closing(database(obj.directory)) as db, db:
        db.execute('INSERT INTO output VALUES (?,?,?)', (sid, 0, raw))
    export = next(r.endpoint for r in make_native_router(auth, obj.runtime).routes if r.path.endswith('/export'))
    response = await export(sid, request(), format='md')
    iterator = response.body_iterator
    assert 'Chat export' in await anext(iterator)
    assert 'first' in await anext(iterator)
    auth.allowed = False
    with pytest.raises(DevError) as error:
        await anext(iterator)
    assert error.value.code == 'LOGIN_REQUIRED'


@pytest.mark.asyncio
async def test_all_projects_search_names_paths_node_and_permissions(service):
    obj,first,sid=service
    first.update(alias='MCP 主项目',device_name='Studio')
    second={**first,'id':'second','root':first['root']+'/two','alias':'ProjectGamma','device_name':'Laptop'}
    hidden={**first,'id':'hidden','alias':'HIDDEN_RECORD_CANARY','allow_tasks':False}
    mappings={p['id']:p for p in [first,second,hidden]};calls=[]
    def project(value,principal):
        calls.append(value)
        p=mappings.get(value)
        if not p:raise DevError('FORBIDDEN','revoked',403)
        return p
    obj.runtime.project=project
    sid2=add(obj,second,title='Other work',updated=5)
    add(obj,hidden,title='HIDDEN_RECORD_CANARY hidden')
    add(obj,first,root='/old-mapping',title='HIDDEN_RECORD_CANARY stale')
    add(obj,first,status='deleted',title='HIDDEN_RECORD_CANARY deleted')
    read=endpoint(obj)
    data=await read(request(),mode='chat')
    assert data['total']==2 and [r['id'] for r in data['sessions']]==[sid2,sid]
    assert calls.count('project')==1
    for query,expected in [('ProjectGamma',sid2),('laptop',sid2),('/two',sid2),('主项目',sid),('studio',sid)]:
        result=await read(request(),q=query,mode='chat')
        assert [r['id'] for r in result['sessions']]==[expected]
    assert (await read(request(),q='HIDDEN_RECORD_CANARY',mode='chat'))['total']==0
    first['allow_tasks']=False
    assert (await read(request(),mode='chat'))['total']==1


@pytest.mark.asyncio
async def test_recent_order_tie_break_and_final_page_recovery(service):
    obj,p,sid=service
    a=add(obj,p,id='a'*32,created=2,updated=100)
    b=add(obj,p,id='b'*32,created=2,updated=100)
    read=endpoint(obj)
    assert [r['id'] for r in (await read(request(),limit=2))['sessions']]==[b,a]
    page=await read(request(),offset=2,limit=2)
    assert page['offset']==2 and page['sessions'][0]['id']==sid
    with closing(database(obj.directory)) as db,db:
        db.execute("UPDATE sessions SET status='deleted' WHERE id=?",(sid,))
    page=await read(request(),offset=2,limit=2)
    assert page['offset']==0 and len(page['sessions'])==2


@pytest.mark.asyncio
async def test_archive_beyond_thousand_rows_is_reachable(service):
    obj,p,_=service
    with closing(database(obj.directory)) as db,db:
        db.executemany('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)',[(f'{i:032x}',p['id'],p['device_id'],p['root'],p['root'],'pi','Archive','exited',2,2,'chat') for i in range(1101)])
    page=await endpoint(obj)(request(),offset=1050,limit=50,mode='chat')
    assert page['offset']==1050 and page['total']==1102 and len(page['sessions'])==50
    assert 'cached_bytes' in page['sessions'][0]


@pytest.mark.asyncio
async def test_search_split_utf8_escaped_json_and_delta_text_not_settings(service):
    obj,p,sid=service
    raw=(json.dumps({'type':'user','text':'中文项目'},ensure_ascii=False)+'\n'+json.dumps({'type':'message','text':'恢复成功'},ensure_ascii=True)+'\n'+json.dumps({'type':'delta','text':'cross-'})+'\n'+json.dumps({'type':'delta','text':'boundary'})+'\n'+json.dumps({'type':'settings','text':'private-setting-canary'})+'\n').encode()
    split=raw.index('中'.encode())+1
    with closing(database(obj.directory)) as db,db:
        db.execute('INSERT INTO output VALUES (?,?,?)',(sid,0,raw[:split]))
        db.execute('INSERT INTO output VALUES (?,?,?)',(sid,split,raw[split:]))
    read=endpoint(obj)
    for q in ['中文','恢复成功','cross-boundary']:
        assert (await read(request(),q=q,mode='chat'))['total']==1
    assert (await read(request(),q='private-setting-canary',mode='chat'))['total']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('action,code',[('chat_catalog','CLI_CATALOG_TIMEOUT'),('chat_prompt','CLI_UNCERTAIN')])
async def test_catalog_disconnect_is_read_retry_but_prompt_remains_uncertain(service,action,code):
    obj,project,_=service
    async def disconnected(value):
        raise ConnectionError('fixture connection lost')
    obj.runtime.connections['device']=SimpleNamespace(native_protocol=1,native_chat_protocol=2,send=disconnected)
    with pytest.raises(DevError) as error:
        await obj.request(action,project,{})
    assert error.value.code==code
    assert not obj.pending


@pytest.mark.asyncio
async def test_history_requires_admin_even_for_empty_results(service):
    obj,_,_=service
    auth=TestAuth();auth.allowed=False
    read=next(r.endpoint for r in make_native_router(auth,obj.runtime).routes if r.path=='/api/native/sessions')
    with pytest.raises(DevError):await read(request(),q='none')
