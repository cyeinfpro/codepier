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
                        json.dumps({'state': 'ready', 'label': label}), created))
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
        result = records.list('workspace', {**project, '_integration_admin': admin}, 1)
        assert [item['label'] for item in result] == ['other-owner' if admin else 'legacy-order']
        assert len(fetched) <= 2  # One strict-binding reject plus the one requested row.
        db.row_factory = sqlite3.Row
        fields = ('project', 'root', 'workspace', 'device') + (() if admin else ('owner',))
        predicates = ' AND '.join(f"json_extract(binding,'$.{field}') IS ?" for field in fields)
        plan = db.execute('EXPLAIN QUERY PLAN SELECT * FROM integration_records '
            'WHERE kind=? AND json_valid(binding) AND ' + predicates,
            ('workspace', *(binding(project)[field] for field in fields))).fetchall()
        assert any('integration_record_scope' in row[3] for row in plan)
        if corrupt_foreign:
            # The additive index can also be created over an existing damaged
            # identity without deleting it or granting access to its payload.
            db.execute('DROP INDEX integration_record_scope')
            Records(journal)
            assert db.execute('SELECT binding FROM integration_records WHERE id=?',
                              (f'{2000:032x}',)).fetchone()[0] == '{bad binding'
    finally:
        db.close()
