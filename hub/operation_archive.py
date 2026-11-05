"""Lossless cold payload storage; never invoked automatically."""
from __future__ import annotations
import base64
import json
import math
import time
import zlib

POINTER = 'archive:v1:'


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS operation_payload_archives (
        operation_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
        body TEXT NOT NULL, output_chars INTEGER NOT NULL,
        archived_at REAL NOT NULL, format_version INTEGER NOT NULL DEFAULT 1)""")


def read_field(store, pointer):
    identifier, field = pointer[len(POINTER):].rsplit(':', 1)
    if field not in {'result', 'output'}:
        raise RuntimeError('Invalid archived payload pointer')
    row = store.one('SELECT body,format_version FROM operation_payload_archives WHERE operation_id=?', (identifier,))
    if row is None or row['format_version'] != 1:
        raise RuntimeError('Archived payload missing or unsupported; restore matching backup')
    raw = zlib.decompress(base64.b64decode(store.decrypt(row['body']), validate=True))
    return json.loads(raw)[field]


def archive_payloads(store, *, now=None, days=30, batch=100, dry_run=True):
    from hub.operation_history import unseal
    now = time.time() if now is None else now
    if type(days) is not int or not 30 <= days <= 36500:
        raise ValueError('Archive retention must be at least 30 days')
    if type(batch) is not int or not 1 <= batch <= 500:
        raise ValueError('Batch must be between 1 and 500')
    if type(dry_run) is not bool or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('Invalid maintenance arguments')
    with store.transaction(immediate=True):
        rows = store.all("""SELECT id,result,output FROM operations o
            WHERE state IN ('succeeded','failed','cancelled') AND updated<?
            AND history_pruned_at IS NULL AND media_expires_at IS NULL
            AND NOT EXISTS (SELECT 1 FROM operation_output_tails t WHERE t.operation_id=o.id)
            AND NOT EXISTS (SELECT 1 FROM operation_payload_archives a WHERE a.operation_id=o.id)
            AND (result IS NOT NULL OR output!='')
            ORDER BY updated,id LIMIT ?""", (now-days*86400, batch))
        if not dry_run:
            for row in rows:
                payload = {field: unseal(store, row[field]) for field in ('result', 'output')}
                raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
                body = store.encrypt(base64.b64encode(zlib.compress(raw)).decode('ascii'))
                store.execute("""INSERT INTO operation_payload_archives
                    (operation_id,body,output_chars,archived_at) VALUES (?,?,?,?)""",
                    (row['id'], body, len(payload['output'] or ''), now))
                prefix = POINTER + row['id'] + ':'
                if any(read_field(store, prefix+field) != payload[field] for field in payload):
                    raise RuntimeError('Archive verification failed')
                store.execute('UPDATE operations SET result=?,output=? WHERE id=?',
                              (prefix+'result', prefix+'output', row['id']))
    return {'operations': len(rows), 'dry_run': dry_run, 'vacuum_performed': False}


def restore_payloads(store, *, batch=100, dry_run=True):
    """Restore inline bodies before downgrading, preserving IDs and timestamps."""
    from hub.operation_history import seal
    if type(batch) is not int or not 1 <= batch <= 500 or type(dry_run) is not bool:
        raise ValueError('Invalid maintenance arguments')
    with store.transaction(immediate=True):
        rows = store.all('SELECT operation_id FROM operation_payload_archives ORDER BY operation_id LIMIT ?', (batch,))
        if not dry_run:
            for row in rows:
                identifier = row['operation_id']
                prefix = POINTER + identifier + ':'
                result, output = (read_field(store, prefix+field) for field in ('result', 'output'))
                store.execute('''UPDATE operations
                    SET result=CASE WHEN result=? THEN ? ELSE result END,
                        output=CASE WHEN output=? THEN ? ELSE output END WHERE id=?''',
                    (prefix+'result', seal(store, result), prefix+'output', seal(store, output), identifier))
                store.execute('DELETE FROM operation_payload_archives WHERE operation_id=?', (identifier,))
    return {'operations': len(rows), 'dry_run': dry_run, 'vacuum_performed': False}
