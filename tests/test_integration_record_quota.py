"""Admission counts active authorized records without decoding retained history."""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from agent.integration_state import Records, binding
from shared.util import DevError


@pytest.fixture
def state():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    journal = SimpleNamespace(db=db, lock=threading.RLock())
    project = {'id': 'fixture', 'root': '/fixture', '_integration_owner': 'owner',
               '_coding_device': 'device'}
    records = Records(journal)
    try:
        yield records, project, db
    finally:
        db.close()


def insert(db, project, bodies, *, start=0, kind='validation', extra=None, canonical=False):
    bound = {**binding(project), **(extra or {})}
    # Legacy key order is authorized just like canonical sorted JSON.
    db.executemany('INSERT INTO integration_records VALUES (?,?,?,?,?)',
        [(kind, f'{index:032x}', json.dumps(bound, sort_keys=canonical), body, index)
         for index, body in enumerate(bodies, start)])


def test_new_record_does_not_materialize_terminal_history(state):
    records, project, db = state
    with db:
        insert(db, project, ['{"state":"passed"}'] * 3000, canonical=True)
        insert(db, project, ['{"state":"executing"}'], start=3000, canonical=True)
    fetched = []

    def observe(cursor, row):
        fetched.append(tuple(row))
        return sqlite3.Row(cursor, row)

    db.row_factory = observe
    queries = []
    db.set_trace_callback(queries.append)
    records.save('validation', 'f' * 32, project, {'state': 'executing'})
    assert len(fetched) == 1, 'only the existing active record should be materialized'
    db.row_factory = sqlite3.Row
    db.set_trace_callback(None)
    quota_query = next(sql for sql in queries if 'json_valid(binding)' in sql)
    plan = db.execute('EXPLAIN QUERY PLAN ' + quota_query).fetchall()
    assert any('integration_record_active_scope' in row[3] for row in plan)
    assert db.execute('SELECT COUNT(*) FROM integration_records').fetchone()[0] == 3002


def test_active_quota_includes_legacy_binding_and_allows_releasing_capacity(state):
    records, project, db = state
    with db:
        insert(db, project, ['{"state":"executing"}'] * 2000)
    with pytest.raises(DevError) as caught:
        records.save('validation', 'f' * 32, project, {'state': 'executing'})
    assert caught.value.code == 'RECORD_QUOTA'
    records.save('validation', '0' * 32, project, {'state': 'passed'}, replace=True)
    records.save('validation', 'f' * 32, project, {'state': 'executing'})
    assert records.load('validation', '0' * 32, project)['state'] == 'passed'


@pytest.mark.parametrize('damaged', ['{broken body', 'null', '[]', '{"state":"unknown"}'])
def test_damaged_or_unknown_own_receipt_counts_as_active_without_blocking_decode(state, damaged):
    records, project, db = state
    with db:
        insert(db, project, ['{"state":"executing"}'] * 1999, canonical=True)
        insert(db, project, [damaged], start=1999, canonical=True)
    with pytest.raises(DevError) as caught:
        records.save('validation', 'f' * 32, project, {'state': 'executing'})
    assert caught.value.code == 'RECORD_QUOTA'
    records.save('validation', '0' * 32, project, {'state': 'passed'}, replace=True)
    records.save('validation', 'f' * 32, project, {'state': 'executing'})
    assert db.execute('SELECT body FROM integration_records WHERE id=?',
                      (f'{1999:032x}',)).fetchone()[0] == damaged


@pytest.mark.parametrize('scope', [
    {'id': 'other'}, {'root': '/other'}, {'_coding_device': 'other'},
    {'_integration_owner': 'other'}, {'_workspace_id': 'other'},
])
def test_admission_keeps_exact_scope_even_for_admin(state, scope):
    records, project, db = state
    with db:
        insert(db, {**project, **scope}, ['{"state":"executing"}'] * 2000)
    records.save('validation', 'f' * 32, {**project, '_integration_admin': True}, {'state': 'executing'})
    assert records.load('validation', 'f' * 32, project)['state'] == 'executing'


def test_unknown_binding_fields_do_not_consume_another_identity_quota(state):
    records, project, db = state
    with db:
        insert(db, project, ['{"state":"executing"}'] * 2000, extra={'unexpected': 'other identity'})
    records.save('validation', 'f' * 32, project, {'state': 'executing'})
    with pytest.raises(DevError):
        records.load('validation', '0' * 32, project)


def test_active_index_can_be_added_without_erasing_damaged_evidence(state):
    records, project, db = state
    with db:
        db.execute('DROP INDEX integration_record_active_scope')
        insert(db, project, ['{broken body'], canonical=True)
        db.execute('INSERT INTO integration_records VALUES (?,?,?,?,?)',
                   ('validation', '1' * 32, '{broken identity', '{broken body', 1))
    Records(records.journal)
    records.save('validation', 'f' * 32, project, {'state': 'executing'})
    assert db.execute('SELECT body FROM integration_records WHERE id=?', ('0' * 32,)).fetchone()[0] == '{broken body'
    assert db.execute('SELECT binding FROM integration_records WHERE id=?', ('1' * 32,)).fetchone()[0] == '{broken identity'
