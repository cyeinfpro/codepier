"""Bounded, read-only traces over the original room and delivery journals.

These projections never decrypt credentials, acknowledge input, reserve a send,
or turn a provider acceptance into proof of delivery or human consumption.
"""
from __future__ import annotations

import json
import re

from hub.collaboration.common import ROOM_INBOX_EVENT, digest, read_cursor, sign_cursor
from hub.collaboration.events import DISPATCH_BUDGETS, DISPATCH_CAPACITY, MAX_ENVELOPE_BYTES, WIRE_HEADER_BYTES
from shared.util import DevError

LIMITS = {'members': 64, 'goals': 32, 'pending_per_member': 100, 'traces': 256,
          'budgets': 34, 'messages': 64, 'parts_per_delivery': 32, 'anomalies': 10, 'relationship_page': 50}
TRACE_LIMITS = {'member': 32, 'message': 32, 'goal': 32, 'work': 48, 'operation': 64, 'delivery': 48, 'reply': 32}


def code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{0,96}', value) else 'unavailable'


def delivery_display(work_state, deliveries):
    """Business completion and transport uncertainty have separate states."""
    if not deliveries:
        return None
    selected = [item for item in deliveries if item.get('state') != 'not_requested']
    if not selected:
        return None
    states = {part.get('state') for item in selected for part in (item.get('parts') or [item])}
    parts = [part for item in selected for part in item.get('parts', [])]
    accepted = sum(part.get('state') == 'accepted' for part in parts)
    confirmed = sum(part.get('state') == 'owner_confirmed' for part in parts)
    unknown = sum(part.get('state') == 'unknown' for part in parts)
    stopped = sum(part.get('state') in {'cancelled', 'rejected', 'superseded'} for part in parts)
    channel = '微信回复' if any(item.get('channel') == 'weixin' for item in selected) else '渠道回复'
    prefix = '已完成，' if work_state == 'completed' else ''
    if unknown or 'unknown' in states:
        progress = f'已受理 {accepted} 段，{unknown} 段待核对。' if accepted and unknown else ''
        return {'state': 'unknown', 'title': prefix + channel + '发送待核对',
                'reason': progress + '原投递结果待核对。请查看需要核对的分项。'}
    if stopped and (accepted or confirmed):
        return {'state': 'partial', 'title': prefix + channel + '部分发送',
                'reason': f'接口受理 {accepted} 段，本人核对 {confirmed} 段，{stopped} 段已停止。'}
    if states & {'waiting_context', 'waiting_media', 'attention', 'needs_attention', 'failed', 'rejected',
                 'blocked', 'cancelled', 'superseded'}:
        return {'state': 'waiting_recovery', 'title': prefix + channel + '待处理',
                'reason': '查看原渠道状态和受影响的分项。'}
    if states & {'queued', 'preparing', 'sending', 'leased', 'pending'}:
        return {'state': 'pending', 'title': prefix + channel + '等待发送',
                'reason': '原投递仍在等待处理。'}
    if states and states <= {'accepted', 'owner_confirmed', 'sent', 'completed'}:
        if 'owner_confirmed' in states:
            title = channel + ('接口受理及本人核对' if 'accepted' in states else '由本人核对收到')
            return {'state': 'owner_confirmed', 'title': title,
                    'reason': f'接口受理 {accepted} 段，本人核对 {confirmed} 段。' if parts else '已保存本人核对记录。'}
        return {'state': 'accepted', 'title': channel + '接口已受理',
                'reason': '平台已受理，这不证明对方已读。'}
    return {'state': 'unavailable', 'title': channel + '状态待核对', 'reason': '无可用的原投递证据。'}


def _notifications(c, room, member, before, limit, *, accepted_only=False):
    """Select by the subscription's original event sequence and join exact identities."""
    generation = c.room_consumers.generation(member)
    condition = "AND d.state='accepted'" if accepted_only else ''
    return c.store.all(f"""SELECT d.id,d.event_id,d.event_seq,d.state,d.attempt,d.created,d.accepted_at,
            d.reason_code,d.next_at,t.expires_at AS ticket_expires_at,
            s.id AS consumer_session_id,s.created AS claimed_at,s.state AS turn_state,
            retained.id AS retained_notification_delivery_id
        FROM mcp_event_deliveries d
        JOIN mcp_event_outbox e ON e.id=d.event_id AND e.conversation_id=?
        LEFT JOIN dot_wake_tickets t ON t.subscription_id=d.subscription_id AND t.delivery_key=d.event_id
            AND t.member_id=? AND t.generation=? AND t.grant_id=e.target_grant_id
        LEFT JOIN dot_consumer_sessions s ON s.id=t.redeemed_session_id
            AND s.member_id=t.member_id AND s.generation=t.generation AND s.grant_id=t.grant_id
        LEFT JOIN mcp_event_deliveries retained ON retained.id=d.coalesced_into_delivery_id
            AND retained.subscription_id=d.subscription_id AND retained.event_seq>d.event_seq
        WHERE d.subscription_id=? AND d.event_seq<? AND e.name=?
          AND json_extract(e.data,'$.member_id')=? AND json_extract(e.data,'$.binding_generation')=?
          AND COALESCE(json_extract(e.data,'$.test'),0)=0
          {condition}
        ORDER BY d.event_seq DESC LIMIT ?""",
        (room['id'], member['id'], generation, member['subscription_id'], before, ROOM_INBOX_EVENT,
         member['id'], member['binding_generation'], limit))


def _wake_evidence(row, now):
    accepted = row['state'] == 'accepted' and row['accepted_at'] is not None
    if row['consumer_session_id']:
        state = 'redeemed'
    elif row['state'] == 'abandoned' and row['attempt'] == 0 and row['reason_code'] == 'mailbox_coalesced':
        state = 'coalesced_before_send'
    elif accepted and row['ticket_expires_at'] is not None:
        state = ('accepted_waiting_claim' if row['ticket_expires_at'] > now
                 else 'accepted_without_observed_claim')
    else:
        state = 'insufficient_evidence'
    return {'notification_delivery_id': row['id'], 'event_id': row['event_id'], 'state': state,
            'callback_state': code(row['state']), 'created_at': row['created'],
            'accepted_at': row['accepted_at'] if accepted else None, 'ticket_expires_at': row['ticket_expires_at'],
            'consumer_session_id': row['consumer_session_id'], 'claimed_at': row['claimed_at'],
            'turn_state': row['turn_state'], 'reason_code': code(row['reason_code']),
            'retained_notification_delivery_id': row['retained_notification_delivery_id'],
            'claim_preceded_recorded_acceptance': bool(
                accepted and row['claimed_at'] is not None and row['claimed_at'] < row['accepted_at'])}


def _reply_origins(c, room, principal, session_ids):
    if not session_ids:
        return {}, False
    limit = LIMITS['relationship_page']
    rows = c.store.all(f"""SELECT m.*,o.consumer_session_id,o.created AS origin_created
        FROM room_reply_origins o INDEXED BY room_reply_origins_session
        JOIN collaboration_messages m ON m.id=o.message_id AND m.conversation_id=?
        WHERE o.consumer_session_id IN ({','.join('?' for _ in session_ids)})
        ORDER BY o.consumer_session_id,o.created,o.message_id LIMIT ?""", (room['id'], *session_ids, limit + 1))
    grouped = {}
    for row in rows[:limit]:
        if c.chatroom.message_visible(principal, row):
            grouped.setdefault(row['consumer_session_id'], []).append(row)
    return grouped, len(rows) > limit


def notification_page(c, room, principal, member, cursor):
    before = 2**63 - 1
    binding = ['room-diagnostic-notifications', principal.user_id, room['id'], member['id'],
               member['binding_generation'], member['subscription_id']]
    if cursor:
        before = read_cursor(c.secret, binding, cursor)
    limit, now = LIMITS['relationship_page'], c.clock()
    rows = _notifications(c, room, member, before, limit + 1)
    selected = rows[:limit]
    session_ids = list(dict.fromkeys(row['consumer_session_id'] for row in selected if row['consumer_session_id']))
    replies, origin_truncated = _reply_origins(c, room, principal, session_ids)
    items = []
    for row in selected:
        item = _wake_evidence(row, now)
        if row['consumer_session_id']:
            origins = replies.get(row['consumer_session_id'], [])
            item.update(first_reply_at=origins[0]['origin_created'] if origins else None,
                        created_reply_count=len(origins), reply_origin_truncated=origin_truncated)
        items.append(item)
    has_more = len(rows) > limit
    return {'schema_version': 1, 'conversation_id': room['id'], 'revision': room['revision'],
            'member_id': member['id'], 'binding_generation': member['binding_generation'],
            'observed_at': now, 'items': items, 'truncated': has_more or origin_truncated,
            'next_cursor': sign_cursor(c.secret, binding, selected[-1]['event_seq']) if has_more else None,
            'counts_are_global_totals': False, 'read_only': True, 'redacted': True, 'grants_authority': False}


def delivery_relationships(c, room, principal, delivery_id):
    from hub.collaboration.weixin_delivery_order import blocked_successors, blocking_predecessors

    store, limit = c.store, LIMITS['relationship_page']
    delivery = store.one("""SELECT m.* FROM weixin_deliveries d JOIN collaboration_messages m
        ON m.id=COALESCE(d.projection_message_id,d.message_id) WHERE d.id=? AND m.conversation_id=?""",
        (delivery_id, room['id']))
    if not delivery or not c.chatroom.message_visible(principal, delivery):
        raise DevError('NOT_FOUND', '当前授权下没有这条原投递', 404)

    def visible(rows):
        ids = tuple(dict.fromkeys(row['message_id'] for row in rows if row['message_id']))
        if not ids:
            return []
        authorized = {row['id'] for row in store.all(f"""SELECT * FROM collaboration_messages
            WHERE conversation_id=? AND id IN ({','.join('?' for _ in ids)})""",
            (room['id'], *ids)) if c.chatroom.message_visible(principal, row)}
        return [row for row in rows if row['message_id'] in authorized]

    def edge(row):
        return {'delivery_id': row['id'], 'message_id': row['message_id'],
                'scope': code(row['scope']), 'reason_code': code(row['reason_code']),
                'decision': code(row['decision'])}

    predecessors = blocking_predecessors(store, delivery_id, limit=limit + 1)
    nearest = predecessors[0] if predecessors else None
    nearest = edge(nearest) if nearest and visible([nearest]) else {'restricted': True} if nearest else None
    roots, current, checked, truncated = [], delivery_id, {delivery_id}, len(predecessors) > limit
    while len(checked) < limit:
        rows = visible(blocking_predecessors(store, current, limit=1))
        if not rows or rows[0]['id'] in checked:
            break
        current = rows[0]['id']
        checked.add(current)
        roots = [edge(rows[0])]
    if len(checked) == limit:
        truncated = True

    direct_rows = blocked_successors(store, delivery_id, limit=limit + 1)
    direct = visible(direct_rows[:limit])
    visited = {row['id'] for row in direct}
    pending = list(visited)
    seen = {delivery_id}
    truncated |= len(direct_rows) > limit
    while pending and len(visited) < limit:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        rows = blocked_successors(store, current, limit=limit + 1)
        truncated |= len(rows) > limit
        for row in visible(rows[:limit]):
            if row['id'] not in visited and row['id'] != delivery_id:
                visited.add(row['id'])
                pending.append(row['id'])
                if len(visited) == limit:
                    truncated = True
                    break
    return {'schema_version': 1, 'conversation_id': room['id'], 'revision': room['revision'],
            'delivery_id': delivery_id, 'direct_blocker': nearest, 'roots': roots,
            'direct_waiting_count': len(direct), 'affected_waiting_count': len(visited),
            'truncated': truncated, 'observed_at': c.clock(), 'counts_are_global_totals': False,
            'read_only': True, 'redacted': True, 'grants_authority': False}


def work_deliveries(c, goal, principal, *, intent_revision, member_id=''):
    """The current canonical result wins over older progress notifications."""
    revision = intent_revision
    publication = c.store.one('SELECT message_id FROM room_goal_publications WHERE goal_id=? AND intent_revision=?',
                             (goal['id'], revision))
    if not publication:
        publication = c.store.one('SELECT message_id FROM team_publications WHERE goal_id=? AND intent_revision=?',
                                 (goal['id'], revision))
    if publication:
        rows = c.store.all('SELECT * FROM collaboration_messages WHERE id=?', (publication['message_id'],))
    else:
        rows = c.store.all('''SELECT * FROM collaboration_messages WHERE goal_id=?
            AND kind IN ('reply','delegation_progress','delegation_result')
            ORDER BY conversation_sequence DESC LIMIT 9''', (goal['id'],))
    member = c.rooms.member(member_id, goal['conversation_id']) if member_id else None
    identifiers = []
    for row in rows[:8]:
        body = json.loads(row['body'])
        if ((not publication and body.get('intent_revision', 1) != revision)
                or not c.chatroom.message_visible(principal, row)
                or (member and not c.room_consumers._visible_message(member, principal, row))):
            continue
        identifiers.append(row['id'])
    grouped = c.channels.for_messages(identifiers)
    return [item for identifier in identifiers for item in grouped.get(identifier, [])], len(rows) > 8


def projection(c, room, principal):
    store, now = c.store, c.clock()
    truncated = {'members': False, 'traces': False, 'budgets': False, 'pending': False, 'goals': False, 'anomalies': False}
    traces, budgets, members, anomalies = [], [], [], []
    queue_data = {key: [] for key in ('waiting_subscription', 'waiting_consumer', 'waiting_resource',
                                    'waiting_input', 'channel_delivery')}
    labels = {'waiting_subscription': '等待宿主订阅', 'waiting_consumer': '待处理原输入',
              'waiting_resource': '等待执行资源', 'waiting_input': '等待补充或恢复',
              'channel_delivery': '等待渠道投递或核对'}

    def age(at):
        return round(now - at, 3) if isinstance(at, (float, int)) and at <= now else None

    def trace(item):
        same_kind = sum(existing['kind'] == item['kind'] for existing in traces)
        if len(traces) < LIMITS['traces'] and same_kind < TRACE_LIMITS[item['kind']]:
            traces.append(item)
        else:
            truncated['traces'] = True

    rows = store.all('SELECT * FROM room_members WHERE conversation_id=? ORDER BY created_at,id LIMIT ?',
                     (room['id'], LIMITS['members'] + 1))
    truncated['members'] = len(rows) > LIMITS['members']
    for member in rows[:LIMITS['members']]:
        state = c.rooms.state(member)
        sub = store.one('SELECT id,state,expires_at,last_accepted FROM mcp_event_subscriptions WHERE id=?',
                        (member['subscription_id'],))
        current = store.one('''SELECT id,state,fence,lease_until,updated FROM dot_consumer_sessions
            WHERE member_id=? AND generation=? ORDER BY fence DESC LIMIT 1''',
                            (member['id'], c.room_consumers.generation(member)))
        if current and current['state'] == 'active' and current['lease_until'] <= now:
            current['state'] = 'expired'
        pending = store.all('''SELECT * FROM dot_mailbox_entries WHERE member_id=? AND generation=?
            AND acknowledged_at IS NULL AND superseded_by='' ORDER BY sequence LIMIT ?''',
                            (member['id'], c.room_consumers.generation(member), LIMITS['pending_per_member'] + 1))
        truncated['pending'] |= len(pending) > LIMITS['pending_per_member']
        visible_pending = []
        for entry in pending[:LIMITS['pending_per_member']]:
            reference = json.loads(entry['reference'])
            message_id = entry['object_id'] if entry['kind'] == 'message' else reference.get('message_id')
            message = store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                                (message_id, room['id'])) if message_id else None
            if message_id and (not message or not c.chatroom.message_visible(principal, message)):
                continue
            goal_id = reference.get('goal_id')
            if goal_id:
                goal = store.one('SELECT * FROM coordination_goals WHERE id=? AND conversation_id=?',
                                 (goal_id, room['id']))
                if not goal:
                    continue
                try:
                    partition = store.one('SELECT * FROM collaboration_rooms WHERE id=?', (goal['room_id'],))
                    c.coordination.authorize(principal, partition, goal, include_archived=room['archived_at'] is not None)
                except DevError:
                    continue
            if entry['kind'] == 'message' and not message:
                continue
            project_id = reference.get('source_project_id')
            if project_id:
                try:
                    c.runtime.project(project_id, principal)
                except DevError:
                    continue
            visible_pending.append(entry)
            if entry['kind'] == 'message' and message:
                queue_data['waiting_consumer'].append(entry['created'])
                trace({'kind': 'message', 'id': message['id'], 'state': 'awaiting_consumer',
                       'member_id': member['id'], 'binding_generation': member['binding_generation'],
                       'message_id': message['id'], 'updated_at': entry['created']})
        wakes = _notifications(c, room, member, 2**63 - 1, 1, accepted_only=True)
        latest_wake = _wake_evidence(wakes[0], now) if wakes else None
        if latest_wake and latest_wake['state'] in {'accepted_without_observed_claim', 'insufficient_evidence'}:
            anomalies.append({'kind': 'notification', 'member_id': member['id'],
                              **latest_wake})
        notification_evidence = None
        if latest_wake and latest_wake.get('accepted_at') is not None:
            notification_evidence = {**latest_wake,
                'state': 'claimed' if latest_wake['state'] == 'redeemed' else 'accepted_unclaimed'}
        notifications = store.one('''SELECT COUNT(*) AS pending_count,MIN(created) AS oldest,
            MIN(next_at) AS next_attempt_at FROM mcp_event_deliveries
            WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')''', (member['subscription_id'],))
        if state in {'invited', 'waiting_host', 'verifying'}:
            queue_data['waiting_subscription'].append(member['created_at'])
        members.append({'member_id': member['id'], 'binding_generation': member['binding_generation'],
            'state': state, 'subscription_verified_at': member['subscription_verified_at'],
            'probe_read_at': member['probe_read_at'], 'probe_replied_at': member['probe_replied_at'],
            'subscription': sub, 'consumer': current,
            'inbox': {'dirty_revision': member['dirty_revision'], 'drained_revision': member['drained_revision'],
                'pending_count': len(visible_pending), 'complete': len(pending) <= LIMITS['pending_per_member'],
                'oldest_age_seconds': age(min((item['created'] for item in visible_pending), default=None))},
            'notification': {'last_accepted_at': sub['last_accepted'] if sub else None, 'read_verified': False,
                'latest_wake': latest_wake, 'correlated_receipt': notification_evidence, 'pending_count': notifications['pending_count'],
                'oldest_age_seconds': age(notifications['oldest']),
                'next_attempt_at': notifications['next_attempt_at']}})
        trace({'kind': 'member', 'id': member['id'], 'member_id': member['id'],
               'binding_generation': member['binding_generation'], 'state': state,
               'updated_at': member['updated_at']})

    goals = store.all('SELECT * FROM coordination_goals WHERE conversation_id=? ORDER BY updated DESC,id LIMIT ?',
                      (room['id'], LIMITS['goals'] + 1))
    truncated['goals'] = len(goals) > LIMITS['goals']
    for goal in goals[:LIMITS['goals']]:
        try:
            work = c.room_runtime.work_view(goal, principal)
        except DevError:
            continue
        intent = store.one('SELECT source_message_id FROM room_work_intents WHERE goal_id=?', (goal['id'],))
        source = intent['source_message_id'] if intent else ''
        source_row = store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                               (source, room['id'])) if source else None
        if not source_row or not c.chatroom.message_visible(principal, source_row):
            source = ''
        member_id = work.get('coordinator_member_id', '')
        lead = store.one('SELECT binding_generation FROM room_members WHERE id=?', (member_id,)) if member_id else None
        common = {'goal_id': goal['id'], 'member_id': member_id, 'message_id': source,
                  **({'binding_generation': lead['binding_generation']} if lead else {})}
        trace({'kind': 'goal', 'id': goal['id'], **common, 'state': work['state'],
               'intent_revision': work['intent_revision'], 'updated_at': goal['updated'],
               'reason_code': code(work.get('reason_code', '')),
               'display': {'title': work['display']['title'], 'reason': work['display']['reason']}})
        if work['state'] in {'blocked', 'waiting_member', 'paused'}:
            queue_data['waiting_input'].append(goal['updated'])
        for item in work['items'][:32]:
            trace({'kind': 'work', 'id': item['id'], **common, 'work_item_id': item['id'],
                   'member_id': item.get('member_id', ''), 'state': item['state'],
                   'reason_code': code(item['reason_code']), 'updated_at': goal['updated']})
        truncated['traces'] |= len(work['items']) > 32
        ops = store.all('''SELECT o.id,o.state,o.cancel_requested,o.created,c.work_item_id,c.project_id
            FROM coordination_operations c JOIN operations o ON o.id=c.operation_id
            WHERE c.goal_id=? ORDER BY c.created DESC,o.id LIMIT 65''', (goal['id'],))
        truncated['traces'] |= len(ops) > 64
        for op in ops[:64]:
            if op['project_id'] not in work['project_ids']:
                continue
            if op['state'] == 'queued':
                queue_data['waiting_resource'].append(op['created'])
            trace({'kind': 'operation', 'id': op['id'], **common, 'operation_id': op['id'],
                   'work_item_id': op['work_item_id'], 'state': op['state'],
                   'cancel_requested': bool(op['cancel_requested']), 'updated_at': op['created']})
        spec = json.loads(goal['spec'])
        budget = spec['budget']
        counters = [{'label': '已准入步骤', 'used': goal['steps'], 'limit': budget['max_steps']},
                    {'label': '协作消息', 'used': goal['messages'], 'limit': budget['max_messages']}]
        budgets.append({'goal_id': goal['id'], 'title': '原工作额度', 'counters': counters,
                        'expires_at': goal['expires_at'], 'reason_code': 'quota_exhausted' if
                        any(item['used'] >= item['limit'] for item in counters) else ''})

    messages = store.all('''SELECT * FROM collaboration_messages WHERE conversation_id=?
        ORDER BY conversation_sequence DESC LIMIT ?''', (room['id'], LIMITS['messages'] + 1))
    originals = {row['id']: row for row in messages[:LIMITS['messages']]
                 if c.chatroom.message_visible(principal, row)}
    channel_map = c.channels.for_messages(originals)
    source_refs = {row['id']: json.loads(row['body']).get('source_message_id') for row in originals.values()}
    source_ids = tuple(dict.fromkeys(value for value in source_refs.values() if value))
    visible_sources = set()
    if source_ids:
        visible_sources = {row['id'] for row in store.all(f"""SELECT * FROM collaboration_messages
            WHERE conversation_id=? AND id IN ({','.join('?' for _ in source_ids)})""",
            (room['id'], *source_ids)) if c.chatroom.message_visible(principal, row)}
    reply_ids = [row['id'] for row in originals.values() if row['kind'] == 'reply']
    origins = {}
    if reply_ids:
        origins = {row['message_id']: row for row in store.all(f"""SELECT message_id,consumer_session_id,
            member_id,binding_generation,created FROM room_reply_origins
            WHERE message_id IN ({','.join('?' for _ in reply_ids)})""", tuple(reply_ids))}
    for message_id in reply_ids:
        message = originals[message_id]
        origin = origins.get(message_id)
        source_id = source_refs[message_id]
        trace({'kind': 'reply', 'id': message_id, 'message_id': message_id, 'updated_at': message['created'],
               'source_message_id': source_id if source_id in visible_sources else '',
               'state': 'saved', 'origin': 'recorded' if origin else 'unknown_legacy',
               **({key: origin[key] for key in ('consumer_session_id', 'member_id', 'binding_generation')}
                  if origin else {})})
    truncated['traces'] |= len(messages) > LIMITS['messages']
    for message_id, deliveries in channel_map.items():
        message = originals.get(message_id)
        if not message:
            continue
        source_id = source_refs[message_id]
        if source_id not in visible_sources:
            source_id = ''
        for delivery in deliveries:
            if delivery['state'] == 'not_requested':
                continue
            parts = delivery.get('parts', [])
            display = delivery_display('', [delivery])
            if delivery['state'] not in {'accepted', 'sent', 'completed', 'owner_confirmed', 'cancelled'}:
                queue_data['channel_delivery'].append(message['created'])
            delivery_id = delivery.get('id')
            if display and display['state'] in {'unknown', 'waiting_recovery', 'partial'}:
                anomalies.append({'kind': 'delivery', 'delivery_id': delivery_id, 'message_id': message_id,
                                  'created_at': message['created'], 'state': display['state'], 'display': display})
            trace({'kind': 'delivery', 'id': delivery_id or message_id, 'delivery_id': delivery_id,
                   'message_id': message_id, 'goal_id': message.get('goal_id') or '',
                   'source_message_id': source_id, 'display': display,
                   'channel': delivery.get('channel', 'unknown'), 'state': code(delivery['state']),
                   'reason_code': code(delivery.get('reason_code', '')),
                   'updated_at': delivery.get('updated'), 'provider_delivery_verified': False, 'read_verified': False,
                   'parts': [{key: part[key] for key in ('id', 'ordinal', 'kind', 'state', 'attempts') if key in part}
                             | {'reason_code': code(part.get('reason_code', ''))}
                             for part in parts[:LIMITS['parts_per_delivery']]],
                   'parts_truncated': len(parts) > LIMITS['parts_per_delivery']})
    anomalies.sort(key=lambda item: item['created_at'])
    truncated['anomalies'] = len(anomalies) > LIMITS['anomalies']

    for name, scope_key in (('global', 'global'), ('owner', 'owner:' + digest([room['space_id'], principal.user_id]))):
        rate, burst, byte_rate, byte_burst = DISPATCH_BUDGETS[name]
        saved = store.one('SELECT * FROM mcp_event_dispatch_budgets WHERE scope_key=?', (scope_key,))
        elapsed = max(0, now - saved['updated']) if saved else 0
        available = min(burst, saved['tokens'] + elapsed * rate) if saved else burst
        available_bytes = min(byte_burst, saved['byte_tokens'] + elapsed * byte_rate) if saved else byte_burst
        headroom = MAX_ENVELOPE_BYTES + WIRE_HEADER_BYTES
        budgets.append({'kind': 'callback_' + name, 'title': '全局通知额度' if name == 'global' else '当前所有者通知额度',
            'counters': [{'label': '当前令牌已用', 'used': round(burst - available, 3), 'limit': burst},
                         {'label': '当前字节令牌已用', 'used': round(byte_burst - available_bytes, 3), 'limit': byte_burst}],
            'rate_per_second': rate, 'byte_rate_per_second': byte_rate,
            'next_available_at': max(now, saved['updated'] if saved else now)
                + max(0, (1 - available) / rate, (headroom - available_bytes) / byte_rate),
            'next_available_basis': 'one_max_envelope_callback_budget_only',
            'reason_code': 'rate_budget_exhausted' if available < 1 else 'byte_budget_exhausted' if available_bytes < headroom else '',
            'capacity_is_authorization': False})
    return {'schema_version': 1, 'conversation_id': room['id'], 'revision': room['revision'], 'state': room['state'],
        'observed_at': now, 'read_only': True, 'redacted': True, 'raw_body_included': False, 'grants_authority': False,
        'members': members, 'traces': traces, 'budgets': budgets[:LIMITS['budgets']],
        'anomalies': anomalies[:LIMITS['anomalies']],
        'queues': [{'kind': name, 'label': labels[name], 'pending_count': len(values),
                    'oldest_age_seconds': age(min(values, default=None))} for name, values in queue_data.items()],
        'scope': 'current_authorized_bounded_window', 'limits': {**LIMITS, 'dispatch_concurrency': DISPATCH_CAPACITY},
        'truncated': truncated, 'unverified': ['external_model_online', 'human_read', 'external_model_call_count'],
        'counts_are_global_totals': False}
