"""Bounded read-only wake context; no consumption, subscription or execution.

An inline page is the same authorized read a consumer could request separately.
Overflow retains the original request. Host scheduling remains unobservable.
"""
from __future__ import annotations

import json

from hub.collaboration.common import MESSAGE_EVENT, canonical

MAX_CONTEXT_BYTES = 65536
MAX_THREAD_PAGES = 2
WORK_PAGE_LIMIT = 10


def wake_context(c, principal, items, work_request, recovery_request, *, first_page):
    context = {'schema_version': 1, 'read_only': True, 'grants_authority': False,
               'generated_at': c.clock(), 'host_wake_at': None,
               'host_scheduling_latency_ms': None, 'max_bytes': MAX_CONTEXT_BYTES,
               'pages': [], 'inbox_pagination_complete': False,
               'instructions': 'Included pages are authorized results of their exact request; read them without repeating that first-page call. '
                   'Read omitted pages from request/thread_request. Follow every next_page_request; for later thread pages, continue '
                   'the original request with next_cursor until empty. Exhaust work and recovery even when message items are empty, '
                   'then rescan resume_request before sleep. Recheck inbox before important actions/final replies. These reads grant '
                   'no authority or receipt and do not replace owner, scope or original-task verification. Recover existing_reply '
                   'instead of repeating an interim reply; publish only within existing authorization.'}
    requests, seen = [], set()
    for item in items:
        request = item['thread_request']
        root = request['arguments']['id']
        if root not in seen:
            seen.add(root)
            requests.append(('thread', root, request))
        if len(seen) >= MAX_THREAD_PAGES:
            break
    if first_page:
        if work_request:
            requests.append(('work', '', {**work_request, 'arguments': {
                **work_request['arguments'], 'limit': WORK_PAGE_LIMIT}}))
        requests.append(('recovery', '', {**recovery_request, 'arguments': {
            **recovery_request['arguments'], 'limit': WORK_PAGE_LIMIT}}))
    # Reserve every fallback before filling a page, so a near-full earlier
    # page cannot push later mandatory request metadata beyond the byte cap.
    context['pages'] = [
        {'kind': kind, 'id': identifier, 'request': request,
         'included': False, 'reason': 'context_byte_budget'}
        for kind, identifier, request in requests]
    for index, (kind, identifier, request) in enumerate(requests):
        # Invoke the existing public read contract. Never reimplement its scope,
        # source-author verification, checkpoint, visibility or lease checks.
        page = c.invoke(request['tool'], request['arguments'], principal)
        next_request = page.get('next_page_request')
        if kind == 'thread' and page.get('next_cursor'):
            next_request = {**request, 'arguments': {**request['arguments'], 'cursor': page['next_cursor']}}
        entry = {'kind': kind, 'id': identifier, 'request': request, 'included': True,
                 'page': page, 'next_page_request': next_request,
                 'resume_request': page.get('resume_request', request)}
        candidate = [*context['pages']]
        candidate[index] = entry
        if len(canonical({**context, 'pages': candidate}).encode('utf-8')) <= MAX_CONTEXT_BYTES:
            context['pages'] = candidate
    return context


def reply_state(c, room, dot, message, principal=None):
    row = c.store.one("""SELECT * FROM collaboration_messages WHERE room_id=?
        AND conversation_id=? AND reply_to_id=? AND origin='bound_connector'
        AND json_extract(body,'$.sender_dot_id')=? ORDER BY created DESC,id DESC LIMIT 1""",
        (room['id'], dot['conversation_id'], message['id'], dot['id']))
    if not row or principal is None or not c.chatroom.message_visible(principal, row):
        return None
    complete = json.loads(row['body']).get('dot_reply_complete') is not False
    return {'message_id': row['id'], 'version': row['version'], 'complete': complete,
            'update_arguments': None if complete else c.dot_relay.update_arguments(room, dot, row)}


def delivery_timing(c, room, dot, receipt):
    accepted = c.store.one("""SELECT MIN(d.accepted_at) AS at FROM mcp_event_deliveries d
        JOIN mcp_event_outbox e ON e.id=d.event_id
        WHERE e.room_id=? AND e.name=? AND json_extract(e.data,'$.message_id')=?
        AND json_extract(e.data,'$.recipient_slot_id')=? AND d.state='accepted'
        AND d.status_code>=200 AND d.status_code<300""",
        (room['id'], MESSAGE_EVENT, receipt['message_id'], dot['id']))['at']
    first = c.store.one("""SELECT MIN(created) AS at FROM collaboration_messages
        WHERE room_id=? AND conversation_id=? AND reply_to_id=? AND origin='bound_connector'
        AND json_extract(body,'$.sender_dot_id')=?""",
        (room['id'], dot['conversation_id'], receipt['message_id'], dot['id']))['at']

    def elapsed(start, end):
        # A late callback receipt may follow a consumer reply. Do not invent a
        # zero or negative latency when the causal ordering is unobservable.
        return round((end - start) * 1000, 3) if start is not None and end is not None and end >= start else None

    return {'saved_at': receipt['created'], 'callback_accepted_at': accepted,
            'callback_state': 'accepted' if accepted is not None else 'not_observed',
            'callback_acceptance_meaning': 'transport_only',
            'consumer_receipt_at': receipt['received_at'], 'first_reply_at': first,
            'message_handled_at': receipt['handled_at'],
            'saved_to_callback_accepted_ms': elapsed(receipt['created'], accepted),
            'callback_accepted_to_consumer_receipt_ms': elapsed(accepted, receipt['received_at']),
            'consumer_receipt_to_first_reply_ms': elapsed(receipt['received_at'], first),
            'saved_to_first_reply_ms': elapsed(receipt['created'], first),
            'host_wake_at': None, 'host_scheduling_latency_ms': None, 'model_online': 'unknown',
            'read_is_consumption': False}
