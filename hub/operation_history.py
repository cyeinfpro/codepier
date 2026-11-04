"""Encrypted history and explicit bounded maintenance, never run at startup."""
from __future__ import annotations
import json
import math
import time

PREFIX = 'history:v1:'


def seal(store, value):
    if value is None or value == '':
        return value
    return PREFIX + store.encrypt(value)


def unseal(store, value):
    if isinstance(value, str) and value.startswith(PREFIX):
        return store.decrypt(value[len(PREFIX):])
    return value


def result(store, row):
    value = unseal(store, row.get('result'))
    return json.loads(value) if value else None


def retention_plan(store, *, now=None, operation_days=30, audit_days=365, batch=100):
    """Bounded read-only review. IDs/fingerprints and unresolved data must remain."""
    if not 1 <= operation_days <= 36500 or not 1 <= audit_days <= 36500:
        raise ValueError('Retention days must be between 1 and 36500')
    now = time.time() if now is None else now
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('A finite observation time is required')
    batch = max(1, min(500, int(batch)))
    rows = store.all("""SELECT id,state,updated,length(result) AS result_bytes,length(output) AS output_bytes
        FROM operations WHERE state IN ('succeeded','failed','cancelled') AND updated<?
        ORDER BY updated,id LIMIT ?""", (now - operation_days * 86400, batch))
    audit = store.all('SELECT id,at FROM audit WHERE at<? ORDER BY at LIMIT ?', (now - audit_days * 86400, batch))
    return {'operations': rows, 'audit': audit, 'automatic_delete_allowed': False,
            'preserve_operation_identity': True, 'approval_required': True}


def same_body(store, before, after):
    """Permit key rotation only when the authenticated plaintext is unchanged."""
    try:
        return int(unseal(store, before) == unseal(store, after))
    except Exception:
        return 0  # A corrupt or unauthenticated body cannot pass immutability.


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(operations)')}
    if 'history_pruned_at' not in columns:
        db.execute('ALTER TABLE operations ADD COLUMN history_pruned_at REAL')
    if 'media_expires_at' not in columns:
        db.execute('ALTER TABLE operations ADD COLUMN media_expires_at REAL')
    # Old JSON expression indexes cannot parse encrypted receipt bodies.
    # This drops only a derived index, never records or media.
    db.execute('DROP INDEX IF EXISTS operations_media_expiry')
    db.execute('CREATE INDEX IF NOT EXISTS operations_sealed_media_expiry ON operations(media_expires_at) WHERE media_expires_at IS NOT NULL')


def _body_reference_predicate():
    # Frozen task receipts, collaboration attempts and published files are
    # durable consumers. Diagnostic operation_events do not consume the body.
    return """NOT EXISTS (SELECT 1 FROM mcp_tasks t WHERE t.task_id=o.id)
        AND NOT EXISTS (SELECT 1 FROM coordination_operations c WHERE c.operation_id=o.id)
        AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.source_operation_id=o.id)
        AND NOT EXISTS (SELECT 1 FROM mcp_event_subscriptions s
            WHERE s.state='active' AND s.expires_at>?
            AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(s.arguments)
                THEN s.arguments ELSE '{}' END,'$.operation_ids') j WHERE j.value=o.id))
        AND NOT EXISTS (SELECT 1 FROM mcp_event_outbox e WHERE e.object_id=o.id)"""


def prune_history(store, *, now=None, operation_days=30, audit_days=365,
                  batch=100, dry_run=True, collaboration=True):
    """Explicit bounded maintenance; never invoked automatically on upgrade.

    IDs, state, fingerprints, idempotency keys and ordering timestamps survive.
    Referenced receipts are deliberately retained even when their consumer is
    terminal. The immediate transaction makes reference checks and scrubbing
    atomic against new bindings. Call only after authorizing actual cleanup.
    """
    now = time.time() if now is None else now
    if type(dry_run) is not bool or type(collaboration) is not bool:
        raise ValueError('Maintenance flags must be explicit booleans')
    if type(batch) is not int or not 1 <= batch <= 500:
        raise ValueError('Batch must be between 1 and 500')
    if type(operation_days) is not int or operation_days < 30 or type(audit_days) is not int or audit_days < 365:
        raise ValueError('Retention requires at least 30 days for bodies and 365 days for audit')
    # Reuse validation, but not its broader review-only candidate list.
    retention_plan(store, now=now, operation_days=operation_days,
                   audit_days=audit_days, batch=batch)
    batch = max(1, min(500, int(batch)))
    counts = {'operation_bodies': 0, 'audit': 0, 'dry_run': bool(dry_run)}
    with store.transaction(immediate=True):
        rows = store.all("""SELECT o.id FROM operations o
            WHERE o.state IN ('succeeded','failed','cancelled')
              AND o.updated<? AND o.history_pruned_at IS NULL AND """ +
            _body_reference_predicate() + " ORDER BY o.updated,o.id LIMIT ?",
            (now - operation_days * 86400, now, batch))
        audits = store.all("""SELECT a.id FROM audit a WHERE a.at<?
            AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.id=a.target
                AND (o.state NOT IN ('succeeded','failed','cancelled')
                    OR NOT (""" + _body_reference_predicate() + """)))
            ORDER BY a.at,a.id LIMIT ?""", (now - audit_days * 86400, now, batch))
        protected = store.one("""SELECT count(*) AS count FROM operations o
            WHERE o.state IN ('succeeded','failed','cancelled') AND o.updated<?
              AND o.history_pruned_at IS NULL AND NOT (""" +
            _body_reference_predicate() + ")", (now-operation_days*86400, now))
        counts.update(operation_bodies=len(rows), audit=len(audits),
                      protected_by_reference=protected['count'],
                      reference_reasons=['frozen_mcp_task', 'coordination_operation',
                                         'artifact', 'active_subscription', 'event_outbox'])
        if not dry_run:
            # A recognizable, encrypted tombstone makes replay return an
            # expired receipt, never execute the operation again.
            tombstone = seal(store, json.dumps({'ok': False, 'error': {
                'code': 'HISTORY_EXPIRED', 'message': 'Operation body retention expired'},
                'history_expired': True}, separators=(',', ':')))
            for row in rows:
                store.execute("""UPDATE operations SET result=?,output='',
                    history_pruned_at=?,media_expires_at=NULL WHERE id=?""",
                    (tombstone, now, row['id']))
                store.execute('DELETE FROM operation_output_tails WHERE operation_id=?', (row['id'],))
            for row in audits:
                store.execute('DELETE FROM audit WHERE id=?', (row['id'],))
        if collaboration:
            from hub.collaboration.history_retention import prune_collaboration_history
            counts['collaboration'] = prune_collaboration_history(
                store, now=now, before=now-operation_days*86400, limit=batch, dry_run=dry_run,
                audit_before=now-audit_days*86400)
    return counts


def encrypt_legacy_history(store, *, batch=100):
    """Explicit resumable, lossless migration, with no startup invocation.

    Frozen snapshots may be re-encrypted only with identical authenticated
    plaintext. Neither receipt meaning nor timestamps/identity change.
    """
    if type(batch) is not int or not 1 <= batch <= 500:
        raise ValueError('Batch must be between 1 and 500')
    counts = {}
    with store.transaction(immediate=True):
        for table, primary, column in (
            ('operations', 'id', 'result'), ('operations', 'id', 'output'),
            ('mcp_tasks', 'task_id', 'terminal_result'),
            ('mcp_tasks', 'task_id', 'terminal_operation')):
            rows = store.all(f"SELECT {primary} AS id,{column} AS body FROM {table} "
                f"WHERE {column} IS NOT NULL AND {column}!='' AND substr({column},1,?)!=? "
                f"ORDER BY {primary} LIMIT ?", (len(PREFIX), PREFIX, batch))
            for row in rows:
                if table == 'operations' and column == 'result':
                    value = json.loads(row['body'])
                    data = value.get('data') if isinstance(value, dict) else None
                    expiry = data.get('computer_expires_at') if isinstance(data, dict) else None
                    if isinstance(expiry, (int, float)) and math.isfinite(expiry):
                        store.execute("""UPDATE operations SET media_expires_at=?
                            WHERE id=? AND (substr(tool,1,9)='computer_' OR tool IN ('browser_snapshot','read'))""",
                            (expiry, row['id']))
                store.execute(f'UPDATE {table} SET {column}=? WHERE {primary}=?',
                              (seal(store, row['body']), row['id']))
            counts[table + '.' + column] = len(rows)
        tails = store.all("""SELECT operation_id,output FROM operation_output_tails
            WHERE (sealed_output IS NULL OR sealed_output='') AND output!=''
            ORDER BY operation_id LIMIT ?""", (batch,))
        for row in tails:
            store.execute("""UPDATE operation_output_tails
                SET sealed_output=?,output_chars=?,output='' WHERE operation_id=?""",
                (seal(store, row['output']), len(row['output']), row['operation_id']))
        counts['operation_output_tails'] = len(tails)
    return counts
