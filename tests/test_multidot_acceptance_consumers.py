"""Cross-component acceptance against public tools and synthetic native callbacks.

These tests verify the server boundary. They do not claim a real Dot host has
resumed a model, persisted an outbox, renewed a subscription, or replied on a phone.
"""
import base64
import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hub.collaboration.common import canonical, digest
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from shared.public_collaboration import request as public_request
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


class NativeCallback:
    def __init__(self):
        self.received = []

    async def __call__(self, url, body, headers):
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        self.received.append((url, payload))
        return Reply(204)


def create_dot(fixture):
    service, owner, actor, _, room, _, clock, scope = fixture
    service.runtime.collaboration = service
    actor = replace(actor, grant_id='broad', actor='mcp:broad:acceptance')
    dot = service.dots.create({**scope, 'conversation_id': room['id'],
        'label': 'Synthetic acceptance Dot', 'capabilities': ['read', 'write', 'execute'],
        'acknowledge_unsandboxed_exec': True, 'confirm_tasks': True,
        'duplex': True, 'idempotency_key': key()}, owner)['dot']
    joined = service.invoke('collaboration', {
        'action': 'join', 'code': dot['join_code'], 'idempotency_key': key()}, actor)
    return SimpleNamespace(service=service, owner=owner, actor=actor, room=room,
                           clock=clock, scope=scope, dot=joined['slot'], joined=joined)


def invoke(c, alias, arguments, *, actor=None):
    request = public_request(alias, arguments)
    assert request['tool'] in {'collaboration', 'collaboration_query', 'collaboration_work'}
    return c.service.invoke(request['tool'], request['arguments'], actor or c.actor)


def scope(c):
    return {**c.scope, 'dot_id': c.dot['id']}


def owner_message(c, text='Discuss this request before doing any work.', **extra):
    return c.service.chatroom.create({**c.scope, 'room_id': c.room['id'],
        'conversation_id': c.dot['conversation_id'], 'body_text': text,
        'mentions': [{'slot_id': c.dot['id']}],
        'client_message_id': key(), 'idempotency_key': key(), **extra}, c.owner)['message']


async def activate(c, receiver=None):
    service = c.service
    service.config = replace(service.config, events_enabled=True)
    receiver = receiver or NativeCallback()
    if not service.events or not isinstance(service.events.sender, NativeCallback):
        service.events = EventService(service, receiver)
    else:
        receiver = service.events.sender
    config = invoke(c, 'collaboration_dot_mailbox_configure', {
        **scope(c), 'confirm_consumer_protocol': True, 'idempotency_key': key()})
    assert config['active_protocol_version'] == 1
    assert service.dot_mailbox.binding(c.dot['id'])['state'] == 'prepared'
    subscription = await service.events.subscribe({**config['subscription_request'],
        'delivery': {'mode': 'webhook',
                     'url': 'https://receiver-' + c.dot['id'] + '.example.invalid/events',
                     'secret': 'whsec_' + base64.b64encode(b'synthetic-acceptance-signing-key!!').decode()}}, c.actor)
    c.generation = config['binding_generation']
    c.subscription = subscription
    c.receiver = receiver
    assert service.dot_mailbox.binding(c.dot['id'])['state'] == 'active'
    return c


async def wake(c, text='Discuss this request before doing any work.'):
    message = owner_message(c, text)
    await c.service.events.tick()
    payloads = [payload['data'] for _, payload in c.receiver.received
                if payload.get('type') != 'verification'
                and payload.get('data', {}).get('recipient_slot_id') == c.dot['id']
                and payload.get('data', {}).get('wake_ticket')]
    assert payloads, 'A committed input must reach the verified synthetic callback.'
    return message, payloads[-1]['wake_ticket']


def begin(c, ticket):
    args = {**scope(c), 'binding_generation': c.generation, 'wake_ticket': ticket,
            'consumer_run_id': key(), 'idempotency_key': key()}
    return invoke(c, 'collaboration_consumer_begin', args), args


def credentials(session):
    return {name: session[name] for name in (
        'consumer_session_id', 'consumer_fence', 'consumer_proof')}


def revision(c):
    return invoke(c, 'collaboration_dot_mailbox', {
        **scope(c), 'binding_generation': c.generation})['dirty_revision']


def complete(c, session, message):
    return invoke(c, 'collaboration_dot_message', {
        **scope(c), **credentials(session), 'expected_inbox_revision': revision(c),
        'reply_to_id': message['id'], 'body_text': 'A synthetic reply; no operation was executed.',
        'complete': True, 'idempotency_key': key()})


@pytest.mark.asyncio
async def test_callback_ticket_public_begin_and_passive_reads_do_not_leak_credentials(collab):
    c = await activate(create_dot(collab))
    message, ticket = await wake(c)
    session, request = begin(c, ticket)
    assert invoke(c, 'collaboration_consumer_begin', request) == {**session, 'replayed': True}
    before = c.service.store.one('SELECT total_changes() AS n')['n']
    replies = [
        invoke(c, 'collaboration_dot_mailbox', {**scope(c), 'binding_generation': c.generation}),
        invoke(c, 'collaboration_consumer_read', scope(c)),
        invoke(c, 'collaboration_dot_inbox', scope(c)),
        invoke(c, 'collaboration_dot_connection', scope(c)),
    ]
    assert c.service.store.one('SELECT total_changes() AS n')['n'] == before
    serialized = canonical(replies)
    assert ticket not in serialized and session['consumer_proof'] not in serialized
    for table in ('collaboration_idempotency', 'mcp_event_outbox',
                  'mcp_event_deliveries', 'dot_consumer_sessions', 'dot_wake_tickets'):
        saved = canonical(c.service.store.all('SELECT * FROM ' + table))
        assert ticket not in saved and session['consumer_proof'] not in saved, table
    assert not c.service.store.all('SELECT * FROM operations')
    assert not c.service.store.all('SELECT * FROM coordination_work')
    complete(c, session, message)


@pytest.mark.asyncio
async def test_same_grant_other_dot_cannot_redeem_ticket_or_use_session(collab):
    a = await activate(create_dot(collab))
    b = await activate(create_dot(collab), a.receiver)
    _, ticket = await wake(a)
    with pytest.raises(DevError) as denied:
        begin(b, ticket)
    assert denied.value.code == 'DOT_WAKE_TICKET_INVALID'
    session, _ = begin(a, ticket)
    with pytest.raises(DevError) as denied:
        invoke(b, 'collaboration_dot_message', {**scope(b), **credentials(session),
            'expected_inbox_revision': revision(b), 'body_text': 'Wrong slot',
            'idempotency_key': key()})
    assert denied.value.code == 'DOT_CONSUMER_PROOF_REQUIRED'
    assert not c_messages(b, 'Wrong slot')


def c_messages(c, text):
    return [row for row in c.service.store.all('SELECT body FROM collaboration_messages')
            if json.loads(row['body']).get('body_text') == text]


@pytest.mark.asyncio
@pytest.mark.parametrize('alias', [
    'collaboration_dot_message', 'collaboration_dot_ack', 'collaboration_dot_task',
])
@pytest.mark.parametrize('expired', [False, True])
async def test_activated_v2_cannot_use_unfenced_legacy_mutations(collab, alias, expired):
    c = await activate(create_dot(collab))
    message, _ = await wake(c)
    args = {**scope(c), 'idempotency_key': key()}
    if alias == 'collaboration_dot_message':
        args.update(body_text='Must require a consumer', reply_to_id=message['id'])
    elif alias == 'collaboration_dot_ack':
        args.update(message_id=message['id'], disposition='read')
    else:
        args.update(message_id=message['id'], expected_version=message['version'], confirm_task=True)
    if expired:
        c.clock[0] = c.service.store.one('SELECT expires_at FROM mcp_event_subscriptions WHERE id=?',
                                       (c.subscription['id'],))['expires_at'] + 1
    before = c.service.store.one('SELECT total_changes() AS n')['n']
    with pytest.raises(DevError) as denied:
        c.service.invoke(alias, args, c.actor)
    assert denied.value.code in {'DOT_CONSUMER_PROOF_REQUIRED', 'DOT_CONSUMER_SUBSCRIPTION_INACTIVE'}
    assert c.service.store.one('SELECT total_changes() AS n')['n'] == before
    assert not c.service.store.all('SELECT * FROM coordination_work')


@pytest.mark.asyncio
async def test_dirty_race_on_each_side_of_yield_preserves_new_input_and_fences_old_writer(collab):
    c = await activate(create_dot(collab))
    first, ticket = await wake(c)
    session, _ = begin(c, ticket)
    complete(c, session, first)
    observed = revision(c)
    second = owner_message(c, 'New input committed before the yield.')
    more = invoke(c, 'collaboration_consumer_yield', {**scope(c), **credentials(session),
        'expected_inbox_revision': observed, 'idempotency_key': key()})
    assert more['more'] and more['state'] == 'active'
    assert more['inbox_revision'] > observed
    complete(c, session, second)
    drained = invoke(c, 'collaboration_consumer_yield', {**scope(c), **credentials(session),
        'expected_inbox_revision': revision(c), 'idempotency_key': key()})
    assert not drained['more'] and drained['state'] == 'yielded'
    third, next_ticket = await wake(c, 'New input committed after the yield.')
    following, _ = begin(c, next_ticket)
    assert following['consumer_fence'] > session['consumer_fence']
    with pytest.raises(DevError) as denied:
        complete(c, session, third)
    assert denied.value.code == 'DOT_CONSUMER_LEASE_EXPIRED'
    complete(c, following, third)
    assert not c.service.store.all('SELECT * FROM operations')


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['consumer_fence', 'expected_inbox_revision'])
async def test_public_consumer_rejects_boolean_fence_and_revision(collab, field):
    c = await activate(create_dot(collab))
    _, ticket = await wake(c)
    session, _ = begin(c, ticket)
    args = {**scope(c), **credentials(session), 'expected_inbox_revision': revision(c),
            'idempotency_key': key(), field: True}
    with pytest.raises(DevError) as denied:
        invoke(c, 'collaboration_consumer_yield', args)
    assert denied.value.code == 'INVALID_ARGUMENTS'


@pytest.mark.parametrize('first_module', [
    'shared.dot_consumer_contracts', 'shared.dot_mailbox_contracts', 'shared.team_collaboration_contracts',
])
def test_extension_contracts_import_in_any_entry_order_and_reach_catalog(first_module):
    code = """
import importlib
import sys
importlib.import_module(sys.argv[1])
import json
from shared import collaboration_contracts as c
from shared.public_collaboration import EXTENSION_MODELS, LEGACY_TOOLS, request, resolve
from shared.util import DevError
for authorization in ('fixed', 'role'):
    definitions = {d['name']: d for d in c.tool_definitions(authorization)}
    assert set(definitions) == {'collaboration_query', 'collaboration', 'collaboration_work'}
    for name in EXTENSION_MODELS:
        assert name in c.TOOL_MODELS and name in LEGACY_TOOLS, name
        candidate = request(name, {})
        definition = definitions[candidate['tool']]
        action = candidate['arguments']['action']
        assert json.dumps(action) in json.dumps(definition['inputSchema']), action
        assert action in definition['description'], action
for action in ('team_create', 'team_control', 'team_revise_intent'):
    assert 'collaboration_' + action not in LEGACY_TOOLS
    try:
        resolve('collaboration', {'action': action})
    except DevError as error:
        assert error.code == 'INVALID_ARGUMENTS'
    else:
        raise AssertionError('Owner-only action appeared in MCP: ' + action)
from hub.collaboration.service import CollaborationService
"""
    result = subprocess.run([sys.executable, '-c', code, first_module],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_team_source_and_handoff_use_v2_public_boundary_without_duplicate_work(collab):
    a = await activate(create_dot(collab))
    b = await activate(create_dot(collab), a.receiver)
    teams = a.service.coordination.teams
    approved = teams.create({**a.scope, 'conversation_id': a.dot['conversation_id'],
        'label': 'Explicit synthetic team', 'dot_ids': [a.dot['id'], b.dot['id']],
        'coordinator_dot_id': a.dot['id'], 'project_ids': ['proj'], 'capabilities': ['read'],
        'confirm_tasks': True, 'idempotency_key': key()}, a.owner)['team']
    text = 'Review this request together under the explicitly selected team.'
    before = {table: len(a.service.store.all('SELECT * FROM ' + table))
              for table in ('collaboration_messages', 'team_message_bindings', 'coordination_goals')}
    with pytest.raises(DevError) as denied:
        owner_message(a, text, team_id=approved['id'],
                      mentions=[{'slot_id': a.dot['id']}, {'slot_id': b.dot['id']}])
    assert denied.value.code == 'TEAM_COORDINATOR_MENTION_REQUIRED'
    assert before == {table: len(a.service.store.all('SELECT * FROM ' + table)) for table in before}
    source = owner_message(a, text, team_id=approved['id'])
    await a.service.events.tick()
    delivered = [payload['data'] for _, payload in a.receiver.received
                 if payload.get('data', {}).get('recipient_slot_id') == a.dot['id']
                 and payload.get('data', {}).get('wake_ticket')]
    assert delivered
    session, _ = begin(a, delivered[-1]['wake_ticket'])
    args = {**a.scope, 'dot_id': a.dot['id'], **credentials(session),
        'expected_inbox_revision': revision(a), 'team_id': approved['id'],
        'message_id': source['id'], 'source_version': source['version'],
        'source_digest': digest(text), 'idempotency_key': key()}
    goal = invoke(a, 'collaboration_team_from_message', args)['goal']
    repeated = invoke(a, 'collaboration_team_from_message', {
        **args, 'expected_inbox_revision': revision(a), 'idempotency_key': key()})
    assert repeated['goal']['id'] == goal['id']
    assert len(a.service.store.all('SELECT * FROM coordination_goals')) == 1
    # The personal, pre-v2 compatibility route must respect owner team selection.
    with pytest.raises(DevError) as denied:
        invoke(a, 'collaboration_dot_task', {**scope(a), **credentials(session),
            'expected_inbox_revision': revision(a), 'message_id': source['id'],
            'expected_version': source['version'], 'confirm_task': True, 'idempotency_key': key()})
    assert denied.value.code == 'TEAM_SOURCE_REQUIRES_TEAM'
    assert not a.service.store.all('SELECT * FROM delegation_requests')
    assert len(a.service.store.all('SELECT * FROM coordination_goals')) == 1
    handoff_args = {**a.scope, **credentials(session), 'expected_inbox_revision': revision(a),
        'goal_id': goal['id'], 'coordinator_epoch': 1, 'expected_intent_revision': 1,
        'recipient_dot_id': b.dot['id'], 'purpose': 'Review the original request constraints.',
        'idempotency_key': key()}
    handoff = invoke(a, 'collaboration_team_handoff', handoff_args)['handoff']
    replayed = invoke(a, 'collaboration_team_handoff', handoff_args)
    assert replayed['handoff']['id'] == handoff['id']
    with pytest.raises(DevError) as denied:
        invoke(a, 'collaboration_team_handoff', {**handoff_args, 'purpose': 'Changed semantics under one key.'})
    assert denied.value.code == 'IDEMPOTENCY_CONFLICT'
    await a.service.events.tick()
    delivered = [payload['data'] for _, payload in b.receiver.received
                 if payload.get('data', {}).get('recipient_slot_id') == b.dot['id']
                 and payload.get('data', {}).get('wake_ticket')]
    assert delivered, 'The peer handoff must reach the exact second v2 inbox.'
    peer_session, _ = begin(b, delivered[-1]['wake_ticket'])
    mailbox = invoke(b, 'collaboration_dot_mailbox', {
        **scope(b), 'binding_generation': b.generation})
    peer = [entry for entry in mailbox['items'] if entry['kind'] == 'peer_handoff']
    assert len(peer) == 1 and peer[0]['object_id'] == handoff['id']
    with pytest.raises(DevError) as denied:
        invoke(b, 'collaboration_team_handoff', {
            **b.scope, **credentials(peer_session), 'expected_inbox_revision': revision(b),
            'goal_id': goal['id'], 'coordinator_epoch': 1, 'expected_intent_revision': 1,
            'recipient_dot_id': a.dot['id'], 'purpose': 'Forward it back forever.',
            'causation_id': handoff['id'], 'hop_count': 1, 'idempotency_key': key()})
    assert denied.value.code == 'TEAM_HANDOFF_LOOP'
    assert len(a.service.store.all('SELECT * FROM team_handoffs')) == 1
    context = {**a.scope, 'goal_id': goal['id'],
               'coordinator_epoch': 1, 'expected_intent_revision': 1}
    worker_item = invoke(a, 'collaboration_work_create', {
        **context, **credentials(session), 'expected_inbox_revision': revision(a),
        'objective': 'Independently review the source constraints.',
        'acceptance': 'Return the reviewed constraints and their limitations.',
        'target_project_id': 'proj', 'assignee_grant_id': b.actor.grant_id,
        'assignee_dot_id': b.dot['id'], 'idempotency_key': key()})['work_item']

    def finish(member, consumer, item):
        claimed = invoke(member, 'collaboration_work_claim', {
            **context, **credentials(consumer), 'expected_inbox_revision': revision(member),
            'work_item_id': item['id'], 'expected_version': item['version'],
            'idempotency_key': key()})['work_item']
        before_read = member.service.store.one('SELECT total_changes() AS n')['n']
        recovery = invoke(member, 'collaboration_dot_recovery', scope(member))
        assert any(entry['work_item_id'] == item['id'] for entry in recovery['items']), (
            'A new wake must be able to recover already claimed team work from the original Dot.')
        assert member.service.store.one('SELECT total_changes() AS n')['n'] == before_read
        return invoke(member, 'collaboration_work_result', {
            **context, **credentials(consumer), 'expected_inbox_revision': revision(member),
            'work_item_id': item['id'], 'attempt': claimed['attempt'],
            'fencing_token': claimed['fencing_token'], 'outcome': 'succeeded',
            'summary': 'Reviewed the synthetic source; no executable side effect was requested.',
            'operation_ids': [], 'idempotency_key': key()})['work_item']

    worker_result = finish(b, peer_session, worker_item)
    kickoff = a.service.store.one(
        'SELECT * FROM coordination_work WHERE goal_id=? AND id!=?',
        (goal['id'], worker_item['id']))
    coordinator_result = finish(a, session, kickoff)
    refs = [coordinator_result['artifact_ref'], worker_result['artifact_ref']]
    publish_args = {**context, **credentials(session), 'expected_inbox_revision': revision(a),
        'outcome': 'succeeded', 'summary': 'Both server-side review results are available.',
        'artifact_refs': refs, 'idempotency_key': key()}
    publication = invoke(a, 'collaboration_team_publish', publish_args)['publication']
    replay = invoke(a, 'collaboration_team_publish', {
        **publish_args, 'expected_inbox_revision': revision(a), 'idempotency_key': key()})
    assert replay['publication']['message_id'] == publication['message_id']
    final = a.service.store.one('SELECT * FROM collaboration_messages WHERE id=?',
                                (publication['message_id'],))
    assert final['reply_to_id'] == source['id']
    for member, consumer in ((a, session), (b, peer_session)):
        mailbox = invoke(member, 'collaboration_dot_mailbox', {
            **scope(member), 'binding_generation': member.generation, 'limit': 100})
        assert not any(entry['kind'] == 'message' and entry['object_id'] == source['id']
                       and entry['ready'] for entry in mailbox['items']), (
            'The published team source must not force a consumer to spin forever.')
        for entry in mailbox['items']:
            if entry.get('ack_request'):
                acknowledgement = entry['ack_request']
                member.service.invoke(acknowledgement['tool'], {
                    **acknowledgement['arguments'], **credentials(consumer)}, member.actor)
        drained = invoke(member, 'collaboration_consumer_yield', {
            **scope(member), **credentials(consumer), 'expected_inbox_revision': revision(member),
            'idempotency_key': key()})
        assert not drained['more'] and drained['state'] == 'yielded'
    assert len(a.service.store.all("SELECT * FROM collaboration_messages WHERE kind='team_result'")) == 1
    assert len(a.service.store.all('SELECT * FROM mcp_event_subscriptions')) == 2
    assert not a.service.store.all('SELECT * FROM operations')


@pytest.mark.asyncio
async def test_new_owner_input_blocks_public_execute_before_operation_admission(collab):
    c = await activate(create_dot(collab))
    source, ticket = await wake(c, 'Read the README and report the relevant constraints.')
    session, _ = begin(c, ticket)
    invoke(c, 'collaboration_dot_task', {
        **scope(c), **credentials(session), 'expected_inbox_revision': revision(c),
        'message_id': source['id'], 'expected_version': source['version'],
        'confirm_task': True, 'idempotency_key': key()})
    item = c.service.store.one('SELECT * FROM coordination_work')
    claimed = invoke(c, 'collaboration_work_claim', {
        **c.scope, **credentials(session), 'expected_inbox_revision': revision(c),
        'goal_id': item['goal_id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'idempotency_key': key()})['work_item']
    observed = revision(c)
    owner_message(c, 'There is additional context; reconcile this before another step.',
                  reply_to_id=source['id'])
    request = public_request('collaboration_work_execute', {
        **c.scope, **credentials(session), 'expected_inbox_revision': observed,
        'goal_id': item['goal_id'], 'work_item_id': item['id'],
        'attempt': claimed['attempt'], 'fencing_token': claimed['fencing_token'],
        'tool': 'read', 'arguments': {'path': 'README.md'}, 'idempotency_key': key()})
    with pytest.raises(DevError) as denied:
        await c.service.runtime.invoke(request['tool'], request['arguments'], c.actor)
    assert denied.value.code == 'DOT_INBOX_CHANGED'
    assert not c.service.store.all('SELECT * FROM operations')
    assert not c.service.store.all('SELECT * FROM coordination_operations')
