"""Read-only call-log routes against isolated SQLite evidence."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from hub.call_log import operation_summary, register_call_log
from shared.util import DevError


class EvidenceStore:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.queries = []
        self.db.executescript('''
            CREATE TABLE operations (id TEXT PRIMARY KEY, project_id TEXT, device_id TEXT,
                tool TEXT, actor TEXT, state TEXT, created REAL, updated REAL,
                args_summary TEXT, error TEXT, attempts INTEGER DEFAULT 1,
                result TEXT, output TEXT, payload TEXT);
            CREATE TABLE projects (id TEXT PRIMARY KEY, alias TEXT);
            CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT);
            INSERT INTO projects VALUES ('p1', 'MCP'), ('p2', 'Other');
            INSERT INTO devices VALUES ('d1', 'Test Agent');
        ''')

    def all(self, sql, args=()):
        self.queries.append(sql)
        return [dict(r) for r in self.db.execute(sql, args)]

    def add(self, identifier, *, created=10, state='succeeded', tool='fs_read', actor='mcp:g:test', project='p1', args=None, output='', result=None):
        self.db.execute('INSERT INTO operations(id,project_id,device_id,tool,actor,state,created,updated,args_summary,error,result,output,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (identifier,project,'d1',tool,actor,state,created,created+5,json.dumps(args or {'path':'README.md'}),None,json.dumps(result),output,'private-encrypted-payload'))
        self.db.commit()


@pytest.fixture
def log_api():
    store = EvidenceStore()
    calls = []
    class Admin:
        def admin(self, request):
            if request.headers.get('X-Test-Admin') != 'yes':
                raise DevError('UNAUTHORIZED', '需要管理员登录', 401)
            return SimpleNamespace(admin=True, actor='panel:test')
    class Runtime:
        def operation(self, identifier, principal):
            rows=store.all('SELECT * FROM operations WHERE id=?',(identifier,))
            if not rows:
                raise DevError('NOT_FOUND','操作不存在',404)
            row=rows[0]
            row['args_summary']=json.loads(row['args_summary'])
            row['result']=json.loads(row['result'])
            return row
        def trace(self, args, principal):
            calls.append('trace-read')
            assert args['after_event_id'] == 0
            return {'current':{'reason':'操作已结束'},'events':[
                {'source':'agent','stage':'accepted','seq':1,'elapsed_ms':0,'at':10},
                {'source':'agent','stage':'executing','seq':2,'elapsed_ms':100,'at':11},
                {'source':'agent','stage':'persisting','seq':3,'elapsed_ms':350,'at':12}]}
        def invoke(self, *args):
            raise AssertionError('Reading call details must not invoke tools or add audit calls')
    runtime=Runtime()
    runtime.store=store
    runtime.diagnostics=runtime
    app=FastAPI()
    @app.exception_handler(DevError)
    async def error_handler(request, exc):
        return JSONResponse({'error':{'code':exc.code,'message':str(exc)}}, status_code=exc.status)
    register_call_log(app,runtime,Admin())
    with TestClient(app) as client:
        yield client,store,calls
    store.db.close()


AUTH={'X-Test-Admin':'yes'}


def test_admin_gate_precedes_evidence_reads(log_api):
    client,store,calls=log_api
    assert client.get('/api/call-log').status_code==401
    assert client.get('/api/call-log/'+'a'*32).status_code==401
    assert not store.queries and not calls


def test_cursor_pages_have_no_tie_duplicates_or_new_row_shifts(log_api):
    client,store,_=log_api
    for letter in 'abc':
        store.add(letter*32,created=10)
    first=client.get('/api/call-log?limit=2',headers=AUTH).json()
    assert [x['id'] for x in first['operations']]==['c'*32,'b'*32]
    store.add('d'*32,created=20)
    second=client.get('/api/call-log',params={'limit':2,'cursor':first['next_cursor']},headers=AUTH).json()
    assert [x['id'] for x in second['operations']]==['a'*32]
    assert second['next_cursor'] is None
    assert client.get('/api/call-log',params={'cursor':first['next_cursor'],'source':'panel'},headers=AUTH).status_code==400


def test_literal_query_and_composable_filters(log_api):
    client,store,_=log_api
    store.add('a'*32,args={'path':'path/a_%.py'})
    store.add('b'*32,args={'path':'path/abcd.py'},actor='panel:admin',project='p2')
    result=client.get('/api/call-log',params={'q':'a_%','project':'p1','source':'mcp','tool':'fs_read'},headers=AUTH).json()
    assert [r['id'] for r in result['operations']]==['a'*32]
    assert client.get('/api/call-log',params={'q':"' OR 1=1 --"},headers=AUTH).json()['operations']==[]


@pytest.mark.parametrize('query,status', [({'cursor':'not-a-cursor'},400),({'limit':101},422),({'limit':0},422),({'status':'invented'},400),({'source':'invented'},400),({'q':'x'*201},422),({'watch':'not-an-id'},400)])
def test_bad_paging_and_filters_are_rejected(log_api,query,status):
    assert log_api[0].get('/api/call-log',params=query,headers=AUTH).status_code==status


def test_old_secrets_are_scrubbed_and_list_does_not_load_payloads(log_api):
    client,store,_=log_api
    store.add('a'*32,tool='shell_exec',args={'command':'curl --token=hidden-command','env':{'CUSTOM':'hidden-env'}},output='hidden-output',result={'content':'private-file-body'})
    result=client.get('/api/call-log',headers=AUTH)
    assert result.status_code==200
    assert 'hidden-' not in result.text and 'private-' not in result.text
    assert 'args_summary' not in result.json()['operations'][0]
    assert all('payload' not in query and 'output' not in query and 'result' not in query for query in store.queries)


def test_detail_keeps_failure_exit_code_and_never_reexecutes(log_api):
    client,store,calls=log_api
    store.add('a'*32,tool='shell_exec',state='failed',args={'command':'curl --password="hidden command"','env':{'CUSTOM':'hidden-env'}},output='token=hidden-output\nfailed',result={'ok':True,'data':{'exit_code':23,'headers':{'X-Key':'hidden-header'}}})
    response=client.get('/api/call-log/'+'a'*32,headers=AUTH)
    data=response.json()
    assert response.status_code==200 and data['state']=='failed' and data['exit_code']==23
    assert data['timing']=={'wait_ms':1000,'execution_ms':250}
    assert 'hidden-' not in response.text and 'hidden command' not in response.text and 'private-encrypted-payload' not in response.text
    assert calls==['trace-read']
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0]==1


def test_watch_updates_rows_that_no_longer_match_status(log_api):
    client,store,_=log_api
    store.add('a'*32,state='failed')
    result=client.get('/api/call-log',params={'status':'running','watch':'a'*32,'include_filters':'false'},headers=AUTH).json()
    assert not result['operations']
    assert result['updates'][0]['state']=='failed'
    assert result['projects']==[] and result['tools']==[]
    assert not any('DISTINCT' in sql for sql in store.queries)


def test_detail_is_bounded_and_preserves_latest_output_tail(log_api):
    client,store,_=log_api
    store.add('a'*32,output='old output\n'*10000+'LATEST SENTINEL\ntoken=hidden-tail',result={'data':{'content':'long body '*20000}})
    result=client.get('/api/call-log/'+'a'*32,headers=AUTH)
    data=result.json()
    assert data['display']['truncated'] and data['output_truncated']
    assert 'LATEST SENTINEL' in data['output'] and 'hidden-tail' not in result.text
    assert len(result.content)<200000


def test_persisted_summary_counts_without_extra_bodies():
    args={'command':'CUSTOM_ENV="hidden-env" pytest -q --password="hidden-secret"','content':'private-file-body','edits':[{'old_text':'private-old','new_text':'private-new'}]}
    summary=operation_summary(args)
    assert summary['_log']['command_chars']==len(args['command'])
    assert summary['_log']['edit_count']==1
    assert 'hidden-' not in json.dumps(summary)
    assert 'private-' not in json.dumps(summary)
    assert 'hidden-env' in args['command']


def test_distribution_contains_call_log_and_agent_redaction_dependency():
    import io
    import zipfile
    from pathlib import Path
    from hub.agent_install import AgentPackage
    from scripts.build_source_bundle import REQUIRED_FILES, include

    root=Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(io.BytesIO(AgentPackage(root).build().content)) as archive:
        assert 'shared/audit_redaction.py' in archive.namelist()
    required={'hub/call_log.py','shared/audit_redaction.py','web/call-log.js','web/call-log.css'}
    assert required <= REQUIRED_FILES
    assert all(include(Path(name),public=True) for name in required | {'docs/CALL_LOG.md'})
