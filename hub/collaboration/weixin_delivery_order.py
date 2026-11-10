"""Shared, recipient-scoped ordering and forward-only backfill for Weixin replies."""

from __future__ import annotations

import json

from hub.collaboration.common import canonical
from hub.collaboration.delivery_compatibility import raise_floor
from shared.util import DevError

ORDERING_VERSION = 2
MIGRATION_NAME = 'weixin_reply_order_v2'
BATCH_SIZE = 200

# All fragments use the fixed aliases prior (predecessor) and d (successor).
# The only cross-generation edge is a recorded, exact logical dependency.
SAME_GENERATION = '''prior.binding_id=d.binding_id
    AND prior.generation=d.generation AND prior.peer_id=d.peer_id'''
EXPLICIT = 'prior.id=d.depends_on_delivery_id'
SAME_RECIPIENT = f'''(({SAME_GENERATION}) OR (
    {EXPLICIT} AND prior.peer_id=d.peer_id
    AND json_extract(NULLIF(d.ordering_evidence,''),'$.recovery_predecessor_id')=prior.id
    AND EXISTS (SELECT 1 FROM weixin_bindings old JOIN weixin_bindings current
        ON old.account_id=current.account_id AND old.conversation_id=current.conversation_id
        WHERE old.id=prior.binding_id AND current.id=d.binding_id)))'''
SAME_SOURCE = '''COALESCE(json_extract(NULLIF(prior.ordering_evidence,''),'$.canonical_input_root_id'),
        NULLIF(prior.source_message_id,'')) =
    COALESCE(json_extract(NULLIF(d.ordering_evidence,''),'$.canonical_input_root_id'),
        NULLIF(d.source_message_id,''))'''
UNRESOLVED = '''prior.ordering_key='' OR d.ordering_key=''
    OR json_extract(NULLIF(prior.ordering_evidence,''),'$.unresolved')=1
    OR json_extract(NULLIF(d.ordering_evidence,''),'$.unresolved')=1'''
RELATION = f'''CASE
    WHEN COALESCE({SAME_RECIPIENT},0)=0 THEN NULL
    WHEN {EXPLICIT} THEN 'explicit_dependency'
    WHEN prior.kind='reply' AND d.kind='reply' AND {SAME_SOURCE} THEN 'same_source'
    WHEN prior.ordering_key!='' AND prior.ordering_key=d.ordering_key THEN 'same_group'
    WHEN prior.kind='reply' AND d.kind='reply' AND ({UNRESOLVED}) THEN 'legacy_relationship_unknown'
    END'''
REQUIRES_SUCCESS = f'''{EXPLICIT}
    AND json_extract(NULLIF(d.ordering_evidence,''),'$.dependency_mode')='requires_success' '''
UNSETTLED = '''prior.state='waiting_media' OR EXISTS (
    SELECT 1 FROM weixin_parts earlier WHERE earlier.delivery_id=prior.id
    AND earlier.state NOT IN ('accepted','owner_confirmed','cancelled','rejected'))'''
FAILED = '''EXISTS (
    SELECT 1 FROM weixin_parts earlier WHERE earlier.delivery_id=prior.id
    AND earlier.state IN ('cancelled','rejected'))'''
PARTS_EXIST = 'EXISTS (SELECT 1 FROM weixin_parts earlier WHERE earlier.delivery_id=prior.id)'
DECISION = f'''CASE
    WHEN ({RELATION}) IS NULL THEN 'ready'
    WHEN ({REQUIRES_SUCCESS}) AND ({FAILED}) THEN 'failed'
    WHEN ({UNSETTLED}) OR (({REQUIRES_SUCCESS}) AND NOT {PARTS_EXIST}) THEN 'waiting'
    ELSE 'ready' END'''
EARLIER = f'''(({EXPLICIT} AND prior.rowid<d.rowid)
    OR ((prior.created,prior.rowid)<(d.created,d.rowid) AND NOT ({EXPLICIT} IS TRUE)))'''

# The recipient prefilter gives SQLite the indexed normal path, while the exact
# id alternative permits the explicitly recorded cross-generation edge.
PEER_PRIOR = f'''prior.peer_id=d.peer_id
    AND ((prior.binding_id=d.binding_id AND prior.generation=d.generation) OR {EXPLICIT})'''
WAITING_PREDECESSOR = f'''EXISTS (
    SELECT 1 FROM weixin_deliveries prior WHERE {PEER_PRIOR} AND {EARLIER}
    AND ({DECISION})='waiting')'''
FAILED_PREDECESSOR = f'''EXISTS (
    SELECT 1 FROM weixin_deliveries prior WHERE {PEER_PRIOR} AND {EARLIER}
    AND ({DECISION})='failed')'''
SCOPE = f'''CASE
    WHEN ({RELATION})='same_group' AND d.ordering_key LIKE 'work:%' THEN 'same_work'
    WHEN ({RELATION})='same_group' AND d.ordering_key LIKE 'thread:%' THEN 'same_thread'
    WHEN ({RELATION})='legacy_relationship_unknown' THEN 'legacy_peer_unknown'
    WHEN ({RELATION})='explicit_dependency' THEN 'explicit_dependency'
    ELSE 'same_source' END'''


def dependency_state(store, delivery_id):
    row = store.one(f'''SELECT CASE WHEN ({FAILED_PREDECESSOR}) THEN 'failed'
        WHEN ({WAITING_PREDECESSOR}) THEN 'waiting' ELSE 'ready' END AS state
        FROM weixin_deliveries d WHERE d.id=?''', (delivery_id,))
    return row['state'] if row else 'failed'


def ordering_reason(key, evidence):
    value = json.loads(evidence) if evidence else {}
    return value.get('reason_code') or ('' if key else 'historical_ordering_unverified')


def blocking_predecessors(store, delivery_id, *, limit=51):
    """Nearest blocking delivery first. The caller applies message visibility."""
    return store.all(f'''SELECT prior.id,COALESCE(prior.projection_message_id,prior.message_id) AS message_id,
        {SCOPE} AS scope,{RELATION} AS reason_code,{DECISION} AS decision
        FROM weixin_deliveries d JOIN weixin_deliveries prior ON {PEER_PRIOR} AND {EARLIER}
        WHERE d.id=? AND ({DECISION}) IN ('waiting','failed')
        ORDER BY prior.created DESC,prior.rowid DESC LIMIT ?''', (delivery_id, limit))


def _successors(store, delivery_id, predicate, limit):
    return store.all(f'''SELECT d.id,COALESCE(d.projection_message_id,d.message_id) AS message_id,
        {SCOPE} AS scope,{RELATION} AS reason_code,{DECISION} AS decision
        FROM weixin_deliveries prior JOIN weixin_deliveries d ON {PEER_PRIOR} AND {EARLIER}
        WHERE prior.id=? AND {SAME_RECIPIENT} AND ({predicate}) AND
        (d.state='waiting_media' OR EXISTS (
            SELECT 1 FROM weixin_parts p WHERE p.delivery_id=d.id
            AND p.state NOT IN ('accepted','owner_confirmed','cancelled','rejected')))
        ORDER BY d.created,d.rowid LIMIT ?''', (delivery_id, limit))


def blocked_successors(store, delivery_id, *, limit=51):
    return _successors(store, delivery_id, f"({DECISION}) IN ('waiting','failed')", limit)


def successors(store, delivery_id, *, limit=51):
    return _successors(store, delivery_id, REQUIRES_SUCCESS, limit)


def _inbound_identity(store, message):
    if not message or message['origin'] != 'external_channel':
        return None
    origin = json.loads(message['body']).get('external_origin', {})
    if origin.get('channel') != 'weixin':
        return None
    key = (origin.get('account_id'), origin.get('generation'), origin.get('peer_id'),
           origin.get('external_message_id'))
    if any(value in (None, '') for value in key):
        return None
    if store.one('''SELECT 1 FROM weixin_inbound_versions
        WHERE message_id=? AND account_id=? AND generation=? AND peer_id=? AND external_id=?''',
        (message['id'], *key)):
        return key
    return None


def canonical_input(store, source):
    """Resolve only the server-proven edit chain, never an entire thread."""
    if not source:
        return None
    if source['origin'] == 'panel_owner' and source['kind'] in {'text', 'reply'}:
        return source['id']
    identity = _inbound_identity(store, source)
    if not identity:
        return None
    current, seen = source, set()
    for _ in range(64):
        parent_id = current['reply_to_id']
        if not parent_id:
            return current['id']
        if parent_id in seen:
            return None
        seen.add(current['id'])
        parent = store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                           (parent_id, source['conversation_id']))
        if _inbound_identity(store, parent) != identity:
            return None
        current = parent
    return None


def _prior_delivery(store, binding, parent_id, predecessor_id):
    if not (parent_id or predecessor_id):
        return None
    return store.one('''SELECT d.* FROM weixin_deliveries d
        JOIN weixin_bindings b ON b.id=d.binding_id
        WHERE b.account_id=? AND b.conversation_id=? AND d.peer_id=?
        AND ((? IS NULL AND d.message_id=?) OR d.id=?)
        ORDER BY (d.binding_id=? AND d.generation=?) DESC,d.message_version DESC,d.created DESC,d.rowid DESC
        LIMIT 1''', (binding['account_id'], binding['conversation_id'], binding['peer_id'],
                     predecessor_id, parent_id, predecessor_id, binding['id'], binding['generation']))


def reply_ordering(store, message, source, binding, *, predecessor_id=None, dependency_mode='after_settled'):
    """Return the v2 group, evidence and original delivery dependency."""
    if dependency_mode not in {'after_settled', 'requires_success'} or (
            dependency_mode == 'requires_success' and not predecessor_id):
        raise ValueError('An explicit delivery is required for a typed success dependency')
    body = json.loads(message['body'])
    parent_id = message['reply_to_id']
    parent = store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                       (parent_id, message['conversation_id'])) if parent_id else None
    actual_source = source
    if parent and (parent['origin'] == 'external_channel'
                   or (parent['origin'] == 'panel_owner' and parent['kind'] in {'text', 'reply'})):
        actual_source = parent
    canonical_id = canonical_input(store, actual_source)
    prior = _prior_delivery(store, binding, parent_id if parent and parent['origin'] == 'bound_connector' else None,
                            predecessor_id)
    if predecessor_id and not prior:
        raise DevError('WEIXIN_DELIVERY_RELATION_INVALID', '原投递依赖不属于当前收件对象及话题', 409)
    evidence = {
        'canonical_input_root_id': canonical_id,
        'input_id': actual_source['id'] if actual_source else None,
        'reply_parent_id': parent_id or None,
        'work_id': message.get('goal_id'),
        'thread_root_id': message.get('thread_root_id'),
        'intent_revision': body.get('intent_revision'),
        'dependency_mode': dependency_mode,
        'rule': 'persisted_reply_source_v2',
    }
    if not canonical_id or (parent_id and not parent):
        evidence.update(unresolved=True, reason_code='reply_relationship_unverified')
    if prior and (prior['binding_id'] != binding['id'] or prior['generation'] != binding['generation']):
        evidence['recovery_predecessor_id'] = prior['id']
    return ('reply:' + canonical_id if canonical_id else ''), canonical(evidence), prior['id'] if prior else None


def status_evidence(source, work_id, intent_revision, observed_at):
    return canonical({'rule': 'status_source_v2', 'input_id': source['id'], 'work_id': work_id,
                      'intent_revision': intent_revision, 'observed_at': observed_at})


def _historical_order(store, row):
    source = store.one('''SELECT m.* FROM collaboration_messages m JOIN weixin_bindings b
        ON b.id=? AND b.conversation_id=m.conversation_id WHERE m.id=?''',
        (row['binding_id'], row['source_message_id']))
    if row['kind'] == 'status' and source and row['ordering_key'] == 'status:' + source['id']:
        return row['ordering_key'], status_evidence(source, source.get('goal_id'), None, row['created']), ORDERING_VERSION
    message = store.one('''SELECT m.* FROM collaboration_messages m JOIN weixin_bindings b
        ON b.id=? AND b.conversation_id=m.conversation_id
        WHERE m.id=? AND m.origin='bound_connector' ''', (row['binding_id'], row['message_id']))
    binding = store.one('SELECT * FROM weixin_bindings WHERE id=?', (row['binding_id'],))
    if message and source and binding:
        original_route = {**binding, 'generation': row['generation'], 'peer_id': row['peer_id']}
        key, encoded, predecessor = reply_ordering(store, message, source, original_route)
        evidence = json.loads(encoded)
        if (key and not evidence.get('unresolved') and not evidence.get('recovery_predecessor_id')
                and predecessor == row['depends_on_delivery_id']):
            # v1 carries only within-generation explicit edges. Never derive a
            # new historical cross-generation association during backfill.
            evidence['migration_reason'] = 'verified_historical_reply'
            return key, canonical(evidence), ORDERING_VERSION
        if evidence.get('canonical_input_root_id'):
            # Keep an existing coarse group when explicit dependency evidence
            # disagrees. The group remains authoritative within v1.
            evidence.update(unresolved=True, reason_code='legacy_dependency_unverified')
            return row['ordering_key'], canonical(evidence), row['ordering_schema_version']
    return (row['ordering_key'], canonical({'unresolved': True, 'reason_code': 'legacy_source_unverified'}),
            row['ordering_schema_version'])


def backfill(store, now):
    """Inspect a bounded batch, deferring in-flight records without losing the cursor."""
    with store.transaction():
        migration = store.one('SELECT * FROM weixin_order_migrations WHERE name=?', (MIGRATION_NAME,))
        cursor = migration['last_delivery_id'] if migration else ''
        rows = store.all('''SELECT * FROM weixin_deliveries WHERE ordering_checked_version<2 AND id>?
            ORDER BY id LIMIT ?''', (cursor, BATCH_SIZE))
        if not rows:
            remaining = store.one('SELECT id FROM weixin_deliveries WHERE ordering_checked_version<2 ORDER BY id LIMIT 1')
            if not remaining and migration and migration['finished_at'] is not None:
                return
        else:
            remaining = True
        if not migration:
            store.execute('INSERT INTO weixin_order_migrations(name,started_at) VALUES (?,?)', (MIGRATION_NAME, now))
        checked = changed = unresolved = 0
        for row in rows:
            held = store.one('''SELECT 1 FROM weixin_parts WHERE delivery_id=?
                AND (state IN ('preparing','sending') OR lease_until>?) LIMIT 1''', (row['id'], now))
            if held:
                continue
            key, evidence, version = _historical_order(store, row)
            if version == ORDERING_VERSION and not changed:
                raise_floor(store)
            updated = store.execute('''UPDATE weixin_deliveries SET ordering_key=?,ordering_evidence=?,
                ordering_schema_version=?,ordering_checked_version=2,ordering_revision=ordering_revision+1
                WHERE id=? AND ordering_revision=? AND ordering_key=? AND version=? AND ordering_checked_version<2''',
                (key, evidence, version, row['id'], row['ordering_revision'], row['ordering_key'], row['version']))
            if updated.rowcount:
                checked += 1
                changed += int(version == ORDERING_VERSION)
                unresolved += int(version != ORDERING_VERSION)
        store.execute('''UPDATE weixin_order_migrations SET last_delivery_id=?,checked_count=checked_count+?,
            changed_count=changed_count+?,unresolved_count=unresolved_count+?,finished_at=?
            WHERE name=?''', (rows[-1]['id'] if rows else '', checked, changed, unresolved,
                              None if remaining else now, MIGRATION_NAME))
