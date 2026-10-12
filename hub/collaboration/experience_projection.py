"""Evidence-only experience projections. No network, mutation or authorization.

Callers must first resolve the original objects through their current access
checks. A projection revision is a rendering hint, never an access credential.
"""
from __future__ import annotations

from hub.collaboration.common import digest

ACTIONS = frozenset({
    'open_original_message', 'inspect_original_receipt', 'resume_enrollment',
    'open_scope_review', 'open_host_confirmation', 'copy_original_join_instruction',
})
TERMINAL_FAILURES = frozenset({'failed', 'blocked', 'cancelled', 'expired'})
CHANNEL_UNCERTAIN = frozenset({'unknown', 'failed', 'rejected', 'waiting_context'})
LABELS = {
    'saved_waiting_dot': '已保存，等待 Dot', 'observed': 'Dot 已接收',
    'processing': '正在处理', 'waiting_input': '等待你补充',
    'waiting_authorization': '需要授权', 'original_operation_uncertain': '正在核对原操作',
    'awaiting_result_publication': '执行已有结果，等待回复',
    'replied': '已回复', 'completed': '已完成',
    'work_completed_acceptance_unverified': '工作完成，验收待核验',
    'result_saved_channel_uncertain': '结果已保存，部分通道待核对',
    'needs_attention': '需要关注', 'waiting_connection': '等待连接',
    'no_response_required': '无需回复', 'legacy_handled': '已处理，历史原因未记录',
    'not_registered': '继续连接', 'waiting_subscription': '已登记，待宿主订阅',
    'ready': '已连接，可接收消息', 'chat_only': '可对话，执行权限未开放',
    'paused': '已暂停', 'revoked': '已撤销', 'expired': '连接已到期',
    'authorization_unavailable': '当前授权不可用',
}


def action(kind, target):
    if kind not in ACTIONS or not target:
        return None
    return {'kind': kind, 'target_ref': target, 'mutates': False}


def finish(value):
    value['schema_version'] = 1
    value['read_only'] = True
    value['grants_authority'] = False
    value['model_online'] = 'unknown'
    value['human_read_verified'] = False
    value['label'] = LABELS.get(value['primary_state'], '状态待核对')
    value['projection_revision'] = digest(value)
    return value


def connection(slot):
    """Only public, already-authorized slot data. Never expose a join code."""
    identifier = slot.get('dot_id') or slot.get('id')
    status, state = slot.get('status'), slot.get('state')
    primary, kind, reason = 'waiting_subscription', 'open_host_confirmation', 'subscription_missing'
    if state in {'revoked', 'expired', 'paused'} or status == 'room_paused':
        primary = 'paused' if status == 'room_paused' else state
        kind, reason = 'resume_enrollment', primary
    elif status == 'authorization_unavailable' or slot.get('blocked_reason'):
        primary, kind, reason = 'authorization_unavailable', 'open_scope_review', 'current_authorization_unavailable'
    elif state != 'registered':
        primary, kind, reason = 'not_registered', 'resume_enrollment', 'original_enrollment_incomplete'
    elif status in {'subscription_verified', 'test_delivered', 'chat_confirmed', 'partial_confirmed'}:
        primary = 'chat_only' if slot.get('task_blocked_reason') else 'ready'
        kind, reason = None, 'active_subscription'
    observations = [r.get('test', {}).get('accepted_at') for r in slot.get('routes', [])
                    if isinstance(r.get('test'), dict) and r['test'].get('state') == 'accepted']
    return finish({'dot_id': identifier, 'primary_state': primary, 'reason_code': reason,
        'evidence_source': 'current_slot_and_subscription', 'observed_at': max(
            [v for v in observations if isinstance(v, (int, float))], default=slot.get('joined_at')),
        'slot_version': slot.get('version'), 'subscription_count': slot.get('subscription_count', 0),
        'next_action': action(kind, identifier)})


def message(source, recipient, *, reply=None, work=None, channels=(), handling=None):
    """Keep consumption, execution, acceptance, publication and transport separate."""
    handling = handling or {}
    state = handling.get('state') or handling.get('handling_state')
    if not state:
        state = 'legacy_handled' if recipient.get('state') == 'handled' else (
            'observed' if recipient.get('received_at') is not None else 'awaiting')
    reply_body = reply.get('body', {}) if reply else {}
    final = bool(reply and reply_body.get('dot_reply_complete') is not False)
    committed = final or state in {'reply_committed', 'covered'}
    reply_id = handling.get('reply_id') or (reply.get('id') if reply else None)
    source_id = source['id']
    primary, reason, target, kind = 'saved_waiting_dot', 'awaiting_consumer', source_id, None
    if recipient.get('state') == 'read':
        primary, reason = 'observed', 'explicit_receipt'
    if recipient.get('state') == 'handled':
        primary, reason = 'legacy_handled', 'legacy_semantics'
    if reply and not final:
        primary, reason = ('saved_waiting_dot', 'consumer_progress_stale') if recipient.get('relay_state') in {
            'waiting_dot', 'waiting_connection'} else ('processing', 'consumer_reported_progress')
    if committed:
        primary, reason, target, kind = 'replied', 'canonical_reply_committed', reply_id, 'open_original_message'
    if state in {'waiting_input', 'waiting_authorization', 'no_response_required'}:
        primary, reason = state, state
        # A consumer's generic authorization report does not identify a native
        # request or prove that CodePier scope must change. Show its explanation.
        kind = 'open_original_message'
        target = reply_id or source_id
    if work:
        target, kind = work['work_item_id'], 'inspect_original_receipt'
        if work.get('execution_state') in TERMINAL_FAILURES:
            primary, reason = 'needs_attention', 'original_work_' + work['execution_state']
        elif work.get('wait_reason') == 'original_operation_uncertain':
            primary, reason = 'original_operation_uncertain', 'original_operation_uncertain'
        elif work.get('wait_reason') == 'awaiting_result_publication':
            primary, reason = 'awaiting_result_publication', 'original_operations_terminal'
        elif work.get('execution_state') == 'succeeded':
            accepted = work.get('acceptance_status') in {'verified', 'passed'}
            primary = 'completed' if accepted else 'work_completed_acceptance_unverified'
            reason = 'work_and_acceptance_verified' if accepted else 'acceptance_unverified'
        elif work.get('operation_count') or work.get('execution_state') in {'running', 'leased'}:
            if work.get('operation_freshness') == 'fresh' or work.get('consumer_freshness') == 'fresh':
                primary, reason = 'processing', 'original_work_active'
            else:
                primary, reason = 'original_operation_uncertain', 'work_evidence_stale'
    if handling.get('evidence_current') is False or handling.get('source_current') is False:
        primary, reason, kind, target = 'needs_attention', 'handling_evidence_stale', 'open_original_message', source_id
    deliveries = [{'channel': item.get('channel', 'unknown'), 'state': item.get('state', 'unknown'),
                   'receipt_ref': item.get('id'), 'read_verified': False,
                   'evidence_source': 'channel_receipt'} for item in channels]
    uncertain = next((item for item in deliveries if item['state'] in CHANNEL_UNCERTAIN), None)
    if committed and uncertain and primary not in {'needs_attention', 'original_operation_uncertain'}:
        primary, reason = 'result_saved_channel_uncertain', 'channel_receipt_uncertain'
        kind, target = 'inspect_original_receipt', uncertain['receipt_ref'] or reply_id
    if primary == 'saved_waiting_dot' and recipient.get('relay_state') == 'waiting_connection':
        primary, reason, kind, target = 'waiting_connection', 'connection_unavailable', 'resume_enrollment', recipient['dot_id']
    observed = [recipient.get('received_at'), recipient.get('handled_at'), handling.get('updated_at'),
                reply_body.get('dot_updated_at'), reply.get('created') if reply else None,
                work.get('observed_at') if work else None]
    return finish({'message_id': source_id, 'dot_id': recipient['dot_id'],
        'primary_state': primary, 'reason_code': reason, 'next_action': action(kind, target),
        'consumption': {'state': 'observed' if recipient.get('received_at') is not None else 'not_observed',
                        'observed_at': recipient.get('received_at')},
        'handling': {'state': state, 'revision': handling.get('revision'), 'reply_id': reply_id,
                     'reply_version': handling.get('reply_version') or (reply.get('version') if reply else None)},
        'execution': {'state': work.get('execution_state'), 'work_item_id': work['work_item_id'],
                      'wait_reason': work.get('wait_reason')} if work else {'state': 'not_applicable'},
        'acceptance': {'state': work.get('acceptance_status', 'unverified')} if work else {'state': 'not_applicable'},
        'publication': {'state': 'committed' if committed else 'pending', 'reply_id': reply_id},
        'channel_deliveries': deliveries, 'evidence_source': 'durable_receipts',
        'observed_at': max([v for v in observed if isinstance(v, (int, float))], default=None)})


def diagnostic(projections, connections):
    """A strict allowlist export: no prose, paths, IDs, URLs or auth material."""
    entries = []
    for item in [*connections, *projections]:
        state = item.get('primary_state')
        if state not in LABELS:
            state = 'unknown'
        entries.append({'object_ref': digest([item.get('message_id'), item.get('dot_id')])[:12],
                        'category': state, 'observed_at': item.get('observed_at'),
                        'evidence_source': 'durable_receipts' if item.get('message_id') else 'current_slot_and_subscription',
                        'next_action_kind': (item.get('next_action') or {}).get('kind')})
    return {'schema_version': 1, 'read_only': True, 'redacted': True, 'entries': entries,
            'scope': 'current_authorized_loaded_objects', 'raw_body_included': False,
            'unverified': ['native_model_online', 'external_client_read', 'host_outbox_installed'],
            'export_is_not_authorization': True}
