"""Bounded, read-only traces over the original room and delivery journals.

These projections never decrypt credentials, acknowledge input, reserve a send,
or turn a provider acceptance into proof of delivery or human consumption.
"""
from __future__ import annotations

import json
import re

from hub.collaboration.common import digest
from hub.collaboration.room_turn_policy import view as turn_policy_view
from hub.collaboration.events import DISPATCH_BUDGETS, DISPATCH_CAPACITY, MAX_ENVELOPE_BYTES, WIRE_HEADER_BYTES
from shared.util import DevError

LIMITS = {'members': 64, 'goals': 32, 'pending_per_member': 100, 'traces': 256,
          'budgets': 34, 'messages': 64, 'parts_per_delivery': 32}
TRACE_LIMITS = {'member': 32, 'message': 32, 'reply': 32, 'goal': 32, 'work': 48, 'operation': 64, 'delivery': 48}


def code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{0,96}', value) else 'unavailable'


def delivery_display(work_state, deliveries):
    """Business completion and transport uncertainty have separate states."""
    if not deliveries:
        return None
    states = {item.get('state') for item in deliveries}
    states.update(part.get('state') for item in deliveries for part in item.get('parts', []))
    weixin = any(item.get('channel') == 'weixin' for item in deliveries)
    channel = '微信回复' if weixin else '渠道回复'
    completed = work_state == 'completed'
    if 'unknown' in states:
        return {'state': 'unknown', 'title': ('已完成，' if completed else '') + channel + '发送待核对',
                'reason': '结果已保存。原投递结果未知，核对原分项前不会自动重发或重跑工作。'}
    if states & {'waiting_context', 'waiting_media', 'attention', 'needs_attention', 'failed', 'rejected', 'blocked', 'cancelled'}:
        return {'state': 'waiting_recovery', 'title': ('已完成，' if completed else '') + channel + '待恢复',
                'reason': '业务结果与投递分别保存；请查看原渠道状态和投递分项。'}
    if states & {'queued', 'preparing', 'sending', 'leased', 'pending'}:
        return {'state': 'pending', 'title': ('已完成，' if completed else '') + channel + '等待发送',
                'reason': '结果已保存，原投递仍在等待处理。'}
    if states - {'accepted', 'owner_confirmed', 'sent', 'completed'}:
        return {'state': 'unavailable', 'title': channel + '状态待核对', 'reason': '请核对原投递记录，尚不能确认平台已接受。'}
    return {'state': 'accepted', 'title': channel + '已有平台回执',
            'reason': '平台接受和人工确认分别记录；这不证明对方已读。'}


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
    truncated = {'members': False, 'traces': False, 'budgets': False, 'pending': False, 'goals': False}
    traces, budgets, members = [], [], []
    queue_data = {key: [] for key in ('waiting_subscription', 'waiting_consumer', 'waiting_resource',
                                    'waiting_input', 'channel_delivery')}
    labels = {'waiting_subscription': '等待宿主订阅', 'waiting_consumer': '待处理原输入',
              'waiting_resource': '等待执行资源', 'waiting_input': '等待补充或恢复',
              'channel_delivery': '等待渠道投递或核对'}

    visible_scopes = {(entry['project_id'], entry['environment_id'])
                      for entry in c.room_context.projects(principal, room)}

    def visible_message(row):
        return (c.chatroom.message_visible(principal, row)
                and c.room_context.message_scopes(row) <= visible_scopes)

    def age(at):
        return round(max(0, now - at), 3) if isinstance(at, (float, int)) else None

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
        sub = store.one('SELECT id,state,expires_at FROM mcp_event_subscriptions WHERE id=?',
                        (member['subscription_id'],))
        current = store.one('''SELECT id,state,fence,lease_until,hard_deadline,turn_policy,policy_revision,
            route_expires_at,route_generation,scope_digest_evidence,updated FROM dot_consumer_sessions
            WHERE member_id=? AND generation=? ORDER BY fence DESC LIMIT 1''',
                            (member['id'], c.room_consumers.generation(member)))
        if (current and current['state'] == 'active'
                and min(current['lease_until'], current['hard_deadline'] or current['lease_until']) <= now):
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
            if message and not visible_message(message):
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
        delivered = store.one('''SELECT MAX(accepted_at) AS accepted_at FROM mcp_event_deliveries
            WHERE subscription_id=? AND state='accepted' ''', (member['subscription_id'],))
        notifications = store.one('''SELECT COUNT(*) AS pending_count,MIN(created) AS oldest,
            MIN(next_at) AS next_attempt_at FROM mcp_event_deliveries
            WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')''', (member['subscription_id'],))
        if state in {'invited', 'waiting_host', 'verifying'}:
            queue_data['waiting_subscription'].append(member['created_at'])
        wake = store.one('''SELECT d.id AS notification_delivery_id,d.event_id,d.accepted_at,
                t.expires_at AS ticket_expires_at,s.created AS claimed_at
            FROM dot_consumer_sessions s
            JOIN dot_wake_tickets t ON t.redeemed_session_id=s.id AND t.member_id=s.member_id
              AND t.generation=s.generation AND t.grant_id=s.grant_id
            LEFT JOIN mcp_event_deliveries d ON d.subscription_id=t.subscription_id AND d.event_id=t.delivery_key
            WHERE s.id=? AND t.redeemed_session_id!='' ORDER BY t.created,t.delivery_key LIMIT 1''', (current['id'],)) if current else None
        members.append({'member_id': member['id'], 'binding_generation': member['binding_generation'],
            'state': state, 'subscription_verified_at': member['subscription_verified_at'],
            'probe_read_at': member['probe_read_at'], 'probe_replied_at': member['probe_replied_at'],
            'subscription': sub, 'consumer': current, 'current_turn_wake': wake,
            'inbox': {'dirty_revision': member['dirty_revision'], 'drained_revision': member['drained_revision'],
                'pending_count': len(visible_pending), 'complete': len(pending) <= LIMITS['pending_per_member'],
                'oldest_age_seconds': age(min((item['created'] for item in visible_pending), default=None))},
            'notification': {'last_accepted_at': delivered['accepted_at'], 'read_verified': False,
                'pending_count': notifications['pending_count'], 'oldest_age_seconds': age(notifications['oldest']),
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
    identifiers = [row['id'] for row in messages[:LIMITS['messages']] if visible_message(row)]
    originals = {row['id']: row for row in messages[:LIMITS['messages']]}
    reply_ids = [identifier for identifier in identifiers if originals[identifier]['kind'] == 'reply']
    origins = {}
    if reply_ids:
        placeholders = ','.join('?' for _ in reply_ids)
        origins = {row['message_id']: row for row in store.all(f'''SELECT o.message_id,o.consumer_session_id,
            o.member_id,o.binding_generation,o.created FROM room_reply_origins o
            JOIN room_members m ON m.id=o.member_id AND m.conversation_id=?
            JOIN dot_consumer_sessions s ON s.id=o.consumer_session_id AND s.member_id=m.id
            WHERE o.message_id IN ({placeholders})''', (room['id'], *reply_ids))}
    for identifier in reply_ids:
        message = originals[identifier]
        source_id = json.loads(message['body']).get('source_message_id', '')
        source = store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                           (source_id, room['id'])) if source_id else None
        origin = origins.get(identifier)
        trace({'kind': 'reply', 'id': identifier, 'message_id': identifier,
               'origin': 'verified' if origin else 'unknown_legacy', 'updated_at': message['created'],
               **({'source_message_id': source_id} if source and visible_message(source) else {}),
               **({key: origin[key] for key in ('consumer_session_id', 'member_id', 'binding_generation')}
                  if origin else {})})
    channel_map = c.channels.for_messages(identifiers)
    truncated['traces'] |= len(messages) > LIMITS['messages']
    for message_id, deliveries in channel_map.items():
        message = originals[message_id]
        for delivery in deliveries:
            parts = delivery.get('parts', [])
            if delivery['state'] not in {'accepted', 'sent', 'completed', 'owner_confirmed', 'cancelled'}:
                queue_data['channel_delivery'].append(message['created'])
            trace({'kind': 'delivery', 'id': delivery['id'], 'delivery_id': delivery['id'],
                   'message_id': message_id, 'goal_id': message.get('goal_id') or '',
                   'source_message_id': json.loads(message['body']).get('source_message_id', ''),
                   'channel': delivery.get('channel', 'unknown'), 'state': code(delivery['state']),
                   'updated_at': delivery.get('updated'), 'provider_delivery_verified': False, 'read_verified': False,
                   'parts': [{key: part[key] for key in ('id', 'ordinal', 'kind', 'state', 'attempts') if key in part}
                             | {'reason_code': code(part.get('reason_code', ''))}
                             for part in parts[:LIMITS['parts_per_delivery']]],
                   'parts_truncated': len(parts) > LIMITS['parts_per_delivery']})

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
        'turn_policy': turn_policy_view(store, room['id']),
        'members': members, 'traces': traces, 'budgets': budgets[:LIMITS['budgets']],
        'queues': [{'kind': name, 'label': labels[name], 'pending_count': len(values),
                    'oldest_age_seconds': age(min(values, default=None))} for name, values in queue_data.items()],
        'scope': 'current_authorized_bounded_window', 'limits': {**LIMITS, 'dispatch_concurrency': DISPATCH_CAPACITY},
        'truncated': truncated, 'unverified': ['external_model_online', 'human_read', 'external_model_call_count'],
        'counts_are_global_totals': False}
