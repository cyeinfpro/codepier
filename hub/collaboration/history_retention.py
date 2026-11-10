"""Bounded payload compaction; IDs, fingerprints and replay fences never expire."""
import json
import math

from shared.util import DevError

RETIRED_RESPONSE = '{"retention_tombstone":"collaboration-response-v1"}'
AUDIT_TOMBSTONE = '{"retention_tombstone":"audit-detail-v1"}'


def replay_response(value):
    """A retired receipt is a conflict, never permission to repeat its side effect."""
    response = json.loads(value)
    if isinstance(response, dict) and response.get('retention_tombstone') == 'collaboration-response-v1':
        reference = {'message_id': response['message_id']} if response.get('message_id') else {}
        raise DevError('IDEMPOTENCY_RESPONSE_EXPIRED',
                       '请求已执行，历史响应已按保留期清理；不会重新执行，请查询当前状态', 409,
                       **reference)
    return response


def retired_room_reply(result):
    # Retain delivery dispositions and original identities after expiration.
    # The original request key and fingerprint remain in the receipt row.
    original = json.loads(result)
    minimum = ('message_id', 'source_message_id', 'binding_generation', 'complete',
               'yielded_at_commit', 'channel_dispositions', 'fingerprint_schema_version')
    return json.dumps({**json.loads(RETIRED_RESPONSE), **{key: original[key] for key in minimum if key in original}},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))


# A paused room alone is not terminal: pending goals/jobs and subscription
# dependencies also prevent compaction, including unknown future state values.
ROOM_SETTLED = """
 EXISTS (SELECT 1 FROM collaboration_rooms r WHERE r.id=t.room_id AND r.state='paused')
 AND NOT EXISTS (SELECT 1 FROM collaboration_jobs j WHERE j.room_id=t.room_id
     AND j.state NOT IN ('succeeded','failed','cancelled','expired','dead_letter'))
 AND NOT EXISTS (SELECT 1 FROM coordination_goals g WHERE g.room_id=t.room_id
     AND g.state NOT IN ('succeeded','completed','failed','cancelled','expired'))
 AND NOT EXISTS (SELECT 1 FROM coordination_work w JOIN coordination_goals g ON g.id=w.goal_id
     WHERE g.room_id=t.room_id AND w.state NOT IN ('succeeded','failed','cancelled','expired'))
 AND NOT EXISTS (SELECT 1 FROM coordination_operations c JOIN coordination_goals g ON g.id=c.goal_id
     LEFT JOIN operations o ON o.id=c.operation_id WHERE g.room_id=t.room_id
     AND (o.id IS NULL OR o.state NOT IN ('succeeded','failed','cancelled')))
 AND NOT EXISTS (SELECT 1 FROM collaboration_goals g WHERE g.room_id=t.room_id
     AND g.state NOT IN ('succeeded','completed','failed','cancelled','expired','closed'))
 AND NOT EXISTS (SELECT 1 FROM mcp_event_subscriptions s WHERE s.room_id=t.room_id
     AND s.state='active')
"""


# Historical receipts are server-produced. Only these two exact, verified
# identity shapes permit backfill; unknown/ambiguous shapes remain untouched.
ROOM_SCOPE_ID = """
 COALESCE((SELECT m.conversation_id FROM room_mutation_scopes m WHERE m.mutation_id=t.id),
   CASE WHEN json_valid(t.result) THEN json_extract(t.result,'$.conversation_id') END,
   (SELECT f.conversation_id FROM room_connect_flows f
    WHERE f.id=CASE WHEN json_valid(t.result) THEN json_extract(t.result,'$.flow_id') END))
"""
CONVERSATION_SETTLED = """
 EXISTS (SELECT 1 FROM conversation_rooms r WHERE r.id=RECEIPT_ROOM_ID
   AND r.state='archived' AND r.archived_at IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM coordination_goals g WHERE g.conversation_id=r.id
       AND g.state NOT IN ('succeeded','completed','failed','cancelled','expired'))
   AND NOT EXISTS (SELECT 1 FROM coordination_work w JOIN coordination_goals g ON g.id=w.goal_id
       WHERE g.conversation_id=r.id AND w.state NOT IN ('succeeded','failed','cancelled','expired'))
   AND NOT EXISTS (SELECT 1 FROM coordination_operations c JOIN coordination_goals g ON g.id=c.goal_id
       LEFT JOIN operations o ON o.id=c.operation_id WHERE g.conversation_id=r.id
       AND (o.id IS NULL OR o.state NOT IN ('succeeded','failed','cancelled')))
   AND NOT EXISTS (SELECT 1 FROM mcp_event_subscriptions s WHERE s.conversation_id=r.id AND s.state='active'))
""".replace('RECEIPT_ROOM_ID', ROOM_SCOPE_ID)


def prune_collaboration_history(store, *, now, before, limit=100, dry_run=False, audit_before=None):
    """Caller owns the transaction. Only obsolete payloads, never ledger rows.

    Retain inbound message references, uncertain/quarantined/retryable input,
    outbox events and consumer cursor/receipt ledgers. Weixin inbound/version
    rows contain fingerprints, not payloads, and are intentionally untouched.
    """
    store.require_transaction()
    if audit_before is None:
        audit_before = now - 365 * 86400
    if (type(audit_before) not in (int, float) or not math.isfinite(audit_before)
            or audit_before > now - 365 * 86400):
        raise ValueError('Audit retention must be at least 365 days')
    if (not all(type(x) in (int, float) and math.isfinite(x) for x in (now, before))
            or before > now - 30 * 86400):
        raise ValueError('Payload retention must be at least 30 days')
    if type(dry_run) is not bool:
        raise ValueError('dry_run must be a boolean')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('Limit must be between 1 and 500')
    # All identifiers/predicates below are internal constants.
    specifications = (
        ('collaboration_idempotency', 'response', RETIRED_RESPONSE, before, ROOM_SETTLED),
        ('telegram_inbox', 'body', '', before,
         "t.state IN ('committed','ignored') AND COALESCE(t.message_id,'')=''"),
        ('channel_inbound_failures', 'body', '', before,
         "t.state='committed' AND t.updated<?"),
        ('mcp_event_deliveries', 'body', None, before, """
         t.state='accepted' AND t.accepted_at<?
         AND NOT EXISTS (SELECT 1 FROM mcp_event_subscriptions s
             WHERE s.id=t.subscription_id AND
             (s.state='active' OR s.ack_seq<t.event_seq OR s.scan_seq<t.event_seq))
         AND NOT EXISTS (SELECT 1 FROM mcp_event_deliveries d
             WHERE d.event_id=t.event_id AND d.state!='accepted')
         """),
        ('collaboration_audit', 'detail', AUDIT_TOMBSTONE, audit_before, ROOM_SETTLED),
        ('room_mutations', 'result', RETIRED_RESPONSE, before, CONVERSATION_SETTLED),
        ('room_runtime_receipts', 'result', RETIRED_RESPONSE, before, '''
         t.action LIKE 'room_reply:%' AND json_valid(t.result)
         AND json_extract(t.result,'$.retention_tombstone') IS NULL AND ('''
         + CONVERSATION_SETTLED.replace(ROOM_SCOPE_ID, 't.conversation_id') + ')'),
        ('dot_consumer_mutations', 'result', RETIRED_RESPONSE, before, """
         EXISTS (SELECT 1 FROM dot_consumer_sessions s WHERE s.id=t.session_id
             AND s.state IN ('yielded','expired') AND s.updated<? AND s.lease_until<?
             AND NOT EXISTS (SELECT 1 FROM mcp_event_subscriptions sub
                 WHERE sub.id=s.subscription_id AND sub.state='active'))
         """),
    )
    counts = {}
    remaining = limit
    for table, column, replacement, cutoff, predicate in specifications:
        counts[table] = 0
        if remaining == 0:
            continue
        params = [cutoff, replacement]
        params.extend([before] * predicate.count('?'))
        params.append(remaining)
        created_column = 'created_at' if table == 'room_mutations' else 'created'
        rows = store.all(f"""SELECT t.rowid AS retention_rowid,t.{column} AS retention_payload FROM {table} t
            WHERE t.{created_column}<? AND t.{column} IS NOT ? AND ({predicate})
            ORDER BY t.{created_column},t.rowid LIMIT ?""", tuple(params))
        for row in rows:
            if not dry_run:
                if table == 'room_mutations':
                    store.execute(f'''INSERT OR IGNORE INTO room_mutation_scopes
                        SELECT t.id,{ROOM_SCOPE_ID} FROM room_mutations t WHERE t.rowid=?''',
                        (row['retention_rowid'],))
                payload = retired_room_reply(row['retention_payload']) if table == 'room_runtime_receipts' else replacement
                store.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',
                              (payload, row['retention_rowid']))
        counts[table] = len(rows)
        remaining -= len(rows)
    return counts
