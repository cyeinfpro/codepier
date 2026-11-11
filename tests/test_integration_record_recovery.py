"""Damaged evidence remains visible without disabling unrelated record summaries."""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from agent.integration_state import Records, binding


@pytest.fixture
def records():
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    records=Records(SimpleNamespace(db=db,lock=threading.RLock()))
    project={'id':'fixture','root':'/fixture','_integration_owner':'owner'}
    try:
        yield records,project,db
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['workspace','validation','browser'])
@pytest.mark.parametrize('damage', ['json','shape','missing_state','wrong_id','fields'])
def test_invalid_summary_is_an_explicit_error_and_healthy_rows_survive(records,kind,damage):
    records,project,db=records
    id_field={'workspace':'workspace_id','validation':'validation_id','browser':'lease_id'}[kind]
    bad_id,good_id='a'*32,'b'*32
    healthy={'state':'ready','path':'/fixture/worktree','label':'fixture','created':1,'expires':100,
             id_field:good_id}
    broken={**healthy,id_field:bad_id}
    if damage=='missing_state':broken.pop('state')
    if damage=='wrong_id':broken[id_field]=good_id
    if damage=='fields':broken.update(path=None,created=None,expires=None)
    raw='{private broken body' if damage=='json' else '[]' if damage=='shape' else json.dumps(broken)
    with db:
        db.execute('INSERT INTO integration_records VALUES (?,?,?,?,?)',
            (kind,bad_id,json.dumps(binding(project)),raw,2))
    records.save(kind,good_id,project,healthy)
    result=records.list_page(kind,project)
    assert result['records']==[healthy]
    assert len(result['record_errors'])==1
    error=result['record_errors'][0]
    assert error['record_id']==bad_id and error['kind']==kind and error['code']=='RECORD_INVALID'
    assert error['active'] is True and 'private broken body' not in str(result)
    assert db.execute('SELECT body FROM integration_records WHERE id=?',(bad_id,)).fetchone()[0]==raw


@pytest.mark.parametrize('admin', [False,True])
def test_record_listing_uses_ordered_index_for_long_history(records,admin):
    records,project,db=records
    bound=json.dumps(binding(project))
    with db:
        db.executemany('INSERT INTO integration_records VALUES (?,?,?,?,?)',
            [('workspace',f'{i:032x}',bound,json.dumps({'state':'removed','path':'/fixture/old'}),i)
             for i in range(3000)])
    records.save('workspace','f'*32,project,{'state':'ready','path':'/fixture/current'})
    queries=[]
    db.set_trace_callback(queries.append)
    page=records.list_page('workspace',{**project,'_integration_admin':admin},1)
    db.set_trace_callback(None)
    assert page['record_errors']==[] and page['records'][0]['workspace_id']=='f'*32
    query=next(q for q in queries if q.startswith('SELECT'))
    plan=[row[3] for row in db.execute('EXPLAIN QUERY PLAN '+query)]
    assert not any('TEMP B-TREE' in item for item in plan)
    assert any('integration_record_list_' in item for item in plan)


def test_legacy_indexes_are_removed_without_changing_receipts():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    journal = SimpleNamespace(db=db, lock=threading.RLock())
    try:
        records = Records(journal)
        project = {'id': 'p', 'root': '/fixture', '_integration_owner': 'owner'}
        records.save('workspace', 'a'*32, project, {'state': 'ready', 'path': '/fixture/workspace'})
        before = dict(db.execute('SELECT * FROM integration_records').fetchone())
        for name in ('integration_record_time','integration_record_binding','integration_record_scope'):
            db.execute('CREATE INDEX '+name+' ON integration_records(kind,created)')
        Records(journal)
        assert dict(db.execute('SELECT * FROM integration_records').fetchone()) == before
        names = {row[1] for row in db.execute('PRAGMA index_list(integration_records)')}
        assert not names & {'integration_record_time','integration_record_binding','integration_record_scope'}
        assert {'integration_record_active_scope','integration_record_list_owner','integration_record_list_project'} <= names
    finally:
        db.close()
