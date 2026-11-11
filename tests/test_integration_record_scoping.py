"""Private record listing scopes SQL before materializing retained payloads."""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from agent.integration_state import Records, binding


@pytest.mark.parametrize('admin', [False, True])
@pytest.mark.parametrize('corrupt_foreign', [False, True])
def test_record_list_is_scoped_bounded_and_keeps_legacy_binding_order(admin, corrupt_foreign):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    journal = SimpleNamespace(db=db, lock=threading.RLock())
    records = Records(journal)
    project = {'id': 'project', 'root': '/synthetic/project', '_integration_owner': 'owner',
               '_coding_device': 'device', '_workspace_id': ''}
    entries = []
    for index in range(300):
        foreign = binding({**project, 'id': f'other-{index}'})
        entries.append(('workspace', f'{index:032x}', json.dumps(foreign),
                        json.dumps({'state': 'ready', 'label': 'private foreign body'}), 100 + index))
    selected = [
        (1000, binding(project), 'current', 10),
        (1001, binding(project), 'legacy-order', 20),
        (1002, binding({**project, '_integration_owner': 'other-owner'}), 'other-owner', 30),
        (1003, {**binding(project), 'unexpected': 'not the same binding'}, 'extra-key', 40),
    ]
    for identifier, bound, label, created in selected:
        # Existing records need not use sorted JSON key order.
        entries.append(('workspace', f'{identifier:032x}', json.dumps(bound),
                        json.dumps({'state': 'ready', 'label': label, 'path': '/fixture/workspace'}), created))
    if corrupt_foreign:
        entries.append(('workspace', f'{2000:032x}', '{bad binding',
                        json.dumps({'state': 'ready', 'label': 'untrusted identity'}), 500))
    try:
        with journal.lock, db:
            db.executemany('INSERT INTO integration_records VALUES (?,?,?,?,?)', entries)
        fetched = []
        def observe(cursor, row):
            if cursor.description and cursor.description[0][0] == 'kind':
                fetched.append(row[1])
            return sqlite3.Row(cursor, row)
        db.row_factory = observe
        page = records.list_page('workspace', {**project, '_integration_admin': admin}, 1)
        assert page['record_errors'] == []
        result = page['records']
        assert [item['label'] for item in result] == ['other-owner' if admin else 'legacy-order']
        assert len(fetched) <= 2  # One strict-binding reject plus the one requested row.
        db.row_factory = sqlite3.Row
        fields = ('project', 'root', 'workspace', 'device') + (() if admin else ('owner',))
        predicates = ' AND '.join(f"json_extract(binding,'$.{field}') IS ?" for field in fields)
        plan = db.execute('EXPLAIN QUERY PLAN SELECT * FROM integration_records '
            'WHERE kind=? AND json_valid(binding) AND ' + predicates,
            ('workspace', *(binding(project)[field] for field in fields))).fetchall()
        assert any('USING INDEX integration_record_' in row[3] for row in plan)
        assert all('SCAN integration_records' not in row[3] for row in plan)
        if corrupt_foreign:
            # The additive index can also be created over an existing damaged
            # identity without deleting it or granting access to its payload.
            db.execute('DROP INDEX integration_record_list_owner')
            Records(journal)
            assert db.execute('SELECT binding FROM integration_records WHERE id=?',
                              (f'{2000:032x}',)).fetchone()[0] == '{bad binding'
    finally:
        db.close()


@pytest.mark.parametrize('corrupt_foreign', [False, True])
def test_project_cleanup_scopes_rows_before_loading_other_projects(corrupt_foreign):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    records = Records(SimpleNamespace(db=db, lock=threading.RLock()))
    project = {'id': 'selected', 'root': '/synthetic/selected', '_integration_owner': 'owner',
               '_coding_device': 'device', '_integration_admin': True}
    selected = [binding(project),
                binding({**project, '_workspace_id': 'workspace', '_integration_owner': 'peer'})]
    entries = [('browser', f'{i:032x}', json.dumps(binding({**project, 'id': f'other-{i}'})),
                json.dumps({'lease_id': f'{i:032x}', 'state': 'ready'}), i)
               for i in range(300)]
    for i, bound in enumerate(selected, 1000):
        entries.append(('browser', f'{i:032x}', json.dumps(bound),
                        json.dumps({'lease_id': f'{i:032x}', 'state': 'ready'}), i))
    for i, changed in enumerate([{'root': '/different'}, {'_coding_device': 'other-device'}], 2000):
        entries.append(('browser', f'{i:032x}', json.dumps(binding({**project, **changed})),
                        '{broken foreign body', i))
    if corrupt_foreign:
        entries.append(('browser', f'{3000:032x}', '{broken binding', '{broken body', 3000))
    try:
        with db:
            db.executemany('INSERT INTO integration_records VALUES (?,?,?,?,?)', entries)
        fetched = []

        def observe(cursor, row):
            if cursor.description and cursor.description[0][0] == 'binding':
                fetched.append(row[0])
            return sqlite3.Row(cursor, row)

        db.row_factory = observe
        result = records.project_record_ids('browser', project)
        assert {identifier for _, identifier in result} == {f'{i:032x}' for i in [1000, 1001]}
        assert {bound.get('_workspace_id', '') for bound, _ in result} == {'', 'workspace'}
        assert {bound['_integration_owner'] for bound, _ in result} == {'owner', 'peer'}
        assert len(fetched) == 2
        db.row_factory = sqlite3.Row
        plan = db.execute("""EXPLAIN QUERY PLAN SELECT binding,body FROM integration_records
            WHERE kind=? AND json_valid(binding) AND json_extract(binding,'$.project') IS ?
            AND json_extract(binding,'$.root') IS ? AND json_extract(binding,'$.device') IS ?""",
            ('browser', 'selected', '/synthetic/selected', 'device')).fetchall()
        assert any('USING INDEX integration_record_' in row[3] for row in plan)
        assert all('SCAN integration_records' not in row[3] for row in plan)
        assert db.execute('SELECT COUNT(*) FROM integration_records').fetchone()[0] == len(entries)
    finally:
        db.close()
