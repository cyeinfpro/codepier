"""Input coverage contracts against isolated, synthetic collaboration fixtures."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from hub.collaboration.common import canonical, digest
from hub.collaboration.message_handling import migrate
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message, send_dot, inbox, no_work
from tests.test_collaboration_dots import call


def source(c, text='A question', reply_to_id=''):
    return owner_message(c, text, reply_to_id=reply_to_id)[0]['message']


def handling(c, message):
    return c.s.message_handling.summaries([message['id']], principal=c.actor)[(c.slot['id'], message['id'])]


def request(c, messages, state='observed', reason='received', **extra):
    dot = c.s.dots.find(c.slot['id'])
    return {'action': 'dot_handle', **c.scope, 'dot_id': dot['id'],
        'handling_schema_version': 1, 'scope_digest': c.s.message_handling.scope_digest(c.room, dot),
        'inputs': [{'message_id': m['id'], 'source_version': m['version'],
            'source_digest': digest(m['body_text']), 'expected_revision': handling(c, m)['revision']}
            for m in messages], 'handling_state': state, 'reason_code': reason, 'idempotency_key': key(), **extra}


def submit(c, raw, actor=None):
    return c.s.invoke('collaboration', raw, actor or c.actor)


def coverage(c, messages, reply, **extra):
    return request(c, messages, 'covered', 'final_reply', reply_id=reply['id'],
                   reply_version=reply['version'], **extra)


def test_read_only_discovery_and_migration_do_not_invent_handling(collab):
    c = duplex(collab)
    message = source(c, 'Thanks')
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    page = inbox(c)
    assert page['handling_capabilities']['max_inputs'] == 20
    assert page['handling_capabilities']['action'] == 'dot_handle'
    item = page['items'][0]
    assert item['handling']['state'] == 'awaiting'
    assert item['handling']['revision'] == 0
    assert item['handling']['observed_at'] is None
    assert item['handling_arguments']['inputs'][0]['source_digest'] == digest('Thanks')
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    c.s.store.execute("UPDATE collaboration_dot_messages SET state='handled' WHERE message_id=?", (message['id'],))
    with c.s.store.transaction():
        migrate(c.s.store.db)
    assert handling(c, message)['state'] == 'legacy_handled'
    assert handling(c, message)['observed_at'] is None
    assert not c.s.store.all('SELECT * FROM dot_message_handling')
    no_work(c)


def test_same_thread_batch_coverage_is_atomic_direct_and_not_delivery(collab):
    c = duplex(collab)
    first = source(c)
    second = source(c, 'Also explain the tradeoff', first['id'])
    reply = send_dot(c, 'Combined final answer', second['id'])
    assert reply['source_handling']['state'] == 'reply_committed'
    raw = coverage(c, [first, second], reply['message'])
    result = submit(c, raw)
    assert [r['handling_state'] for r in result['records']] == ['covered', 'covered']
    assert not result['delivery_verified'] and not result['execution_changed']
    assert submit(c, raw) == result
    assert len(c.s.store.all('SELECT * FROM dot_message_coverage')) == 2
    assert not inbox(c)['items']
    assert all(handling(c, m)['content_completed'] for m in [first, second])
    assert all(not handling(c, m)['delivery_verified'] for m in [first, second])
    assert len(c.s.store.all("SELECT * FROM collaboration_messages WHERE origin='bound_connector'")) == 1
    no_work(c)


def test_revision_conflict_rolls_back_whole_batch_and_replay_checks_payload(collab):
    c = duplex(collab)
    first = source(c)
    second = source(c, 'Followup', first['id'])
    raw = request(c, [first, second])
    second_only = request(c, [second])
    submit(c, second_only)
    with pytest.raises(DevError, match='revision'):
        submit(c, raw)
    assert handling(c, first)['revision'] == 0
    result = submit(c, second_only)
    changed = {**second_only, 'reason_code': 'acknowledgement_only',
               'handling_state': 'no_response_required', 'confirms_no_new_intent': True}
    with pytest.raises(DevError) as exc:
        submit(c, changed)
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    assert submit(c, second_only) == result


@pytest.mark.parametrize('kind', ['source_version', 'source_digest', 'reply_version', 'scope_digest'])
def test_stale_source_reply_and_scope_fail_without_partial_coverage(collab, kind):
    c = duplex(collab)
    first = source(c)
    reply = send_dot(c, 'Answer', first['id'])['message']
    raw = coverage(c, [first], reply)
    if kind.startswith('source_'):
        raw['inputs'][0][kind] = 9 if kind == 'source_version' else '0' * 64
    else:
        raw[kind] = 9 if kind == 'reply_version' else '0' * 64
    with pytest.raises(DevError):
        submit(c, raw)
    assert not c.s.store.all('SELECT * FROM dot_message_coverage')


@pytest.mark.parametrize('kind', ['interim', 'another_dot', 'cross_thread', 'proactive', 'owner_input', 'older_reply'])
def test_only_current_dot_canonical_final_reply_can_cover(collab, kind):
    c = duplex(collab)
    first = source(c)
    reply = send_dot(c, 'Answer', first['id'], complete=kind != 'interim')['message']
    if kind == 'another_dot':
        other = duplex(collab)
        reply = send_dot(other, 'Other dot', first['id'])['message']
    elif kind == 'cross_thread':
        other_input = source(c, 'Unrelated thread')
        reply = send_dot(c, 'Unrelated answer', other_input['id'])['message']
    elif kind == 'proactive':
        reply = send_dot(c, 'Unthreaded answer')['message']
    elif kind == 'owner_input':
        reply = first
    elif kind == 'older_reply':
        first = source(c, 'New requirement after answer', first['id'])
    with pytest.raises(DevError):
        submit(c, coverage(c, [first], reply))
    assert not c.s.store.all('SELECT * FROM dot_message_coverage')


def test_cross_thread_inputs_and_unaddressed_inputs_rejected(collab):
    c = duplex(collab)
    first = source(c)
    other = source(c, 'Different thread')
    reply = send_dot(c, 'Answer', first['id'])['message']
    with pytest.raises(DevError) as exc:
        submit(c, coverage(c, [first, other], reply))
    assert exc.value.code == 'DOT_HANDLING_THREAD_MISMATCH'
    unaddressed = owner_message(c, 'Not addressed', mentions=[])[0]['message']
    raw = coverage(c, [first], reply)
    raw['inputs'][0]['message_id'] = unaddressed['id']
    with pytest.raises(DevError) as exc:
        submit(c, raw)
    assert exc.value.code == 'DOT_MESSAGE_NOT_ADDRESSED'


@pytest.mark.parametrize('state,reason', [('waiting_input', 'question_asked'),
                                          ('waiting_authorization', 'authorization_required')])
def test_waiting_requires_real_interim_explanation_and_keeps_input_pending(collab, state, reason):
    c = duplex(collab)
    first = source(c)
    interim = send_dot(c, 'Please provide the missing decision', first['id'], complete=False)['message']
    raw = request(c, [first], state, reason, reply_id=interim['id'], reply_version=interim['version'])
    submit(c, raw)
    assert inbox(c)['items'][0]['handling']['state'] == state
    assert not handling(c, first)['content_completed']
    assert not c.s.store.all('SELECT * FROM dot_message_coverage')
    # Cached legacy ack must not erase the precise unresolved decision.
    call(c, {'tool': 'collaboration', 'arguments': {'action': 'dot_ack', **c.scope,
         'dot_id': c.slot['id'], 'message_id': first['id'], 'disposition': 'handled', 'idempotency_key': key()}})
    assert inbox(c)['items'][0]['handling']['state'] == state
    no_work(c)


def test_no_response_is_explicit_with_finite_reason_and_no_new_intent_declaration(collab):
    c = duplex(collab)
    first = source(c, 'Thanks')
    assert inbox(c)['items']  # No keyword inference.
    raw = request(c, [first], 'no_response_required', 'acknowledgement_only')
    with pytest.raises(DevError):
        submit(c, raw)
    submit(c, {**raw, 'confirms_no_new_intent': True})
    assert not inbox(c)['items']
    assert handling(c, first)['state'] == 'no_response_required'
    assert not c.s.store.all("SELECT * FROM collaboration_messages WHERE origin='bound_connector'")
    with pytest.raises(DevError):
        submit(c, {**raw, 'reason_code': 'model_guess', 'confirms_no_new_intent': True})
    no_work(c)


@pytest.mark.parametrize('work_state', ['queued', 'running', 'blocked', 'failed', 'cancelled', 'expired'])
def test_text_coverage_never_hides_existing_unfinished_or_failed_work(collab, work_state):
    c = duplex(collab)
    first = source(c, 'Run the approved project checks')
    call(c, inbox(c)['items'][0]['task_request'])
    work = c.s.store.one('SELECT * FROM coordination_work')
    c.s.store.execute('UPDATE coordination_work SET state=? WHERE id=?', (work_state, work['id']))
    assert handling(c, first)['state'] == 'work_linked'
    reply = send_dot(c, 'Report including unresolved work', first['id'])['message']
    before = c.s.store.one('SELECT * FROM coordination_work')
    for raw in [coverage(c, [first], reply),
                request(c, [first], 'no_response_required', 'social_only', confirms_no_new_intent=True)]:
        with pytest.raises(DevError) as exc:
            submit(c, raw)
        assert exc.value.code == 'DOT_HANDLING_WORK_UNFINISHED'
    assert c.s.store.one('SELECT * FROM coordination_work') == before
    summary = handling(c, first)
    assert summary['requires_attention'] and not summary['content_completed']
    assert summary['work_item_id'] == work['id'] and summary['work_state'] == work_state
    assert not c.s.store.all('SELECT * FROM dot_message_coverage')


def test_work_linked_only_references_original_work_and_changes_no_execution(collab):
    c = duplex(collab)
    first = source(c, 'Run checks')
    call(c, inbox(c)['items'][0]['task_request'])
    work = c.s.store.one('SELECT * FROM coordination_work')
    raw = request(c, [first], 'work_linked', 'existing_work', work_item_id=work['id'])
    submit(c, raw)
    with pytest.raises(DevError):
        submit(c, {**request(c, [first], 'work_linked', 'existing_work'), 'work_item_id': 'unrelated'})
    assert c.s.store.one('SELECT * FROM coordination_work') == work
    assert not c.s.store.all('SELECT * FROM operations')


def test_source_revision_change_invalidates_old_evidence_and_reappears_in_inbox(collab):
    c = duplex(collab)
    first = source(c)
    reply = send_dot(c, 'Answer', first['id'])['message']
    submit(c, coverage(c, [first], reply))
    raw = coverage(c, [first], reply)
    stored = c.s.object('collaboration_messages', c.room, first['id'])
    body = {**json.loads(stored['body']), 'body_text': 'A new task with changed constraints'}
    c.s.store.execute('UPDATE collaboration_messages SET body=?,version=version+1 WHERE id=?',
                      (canonical(body), first['id']))
    summary = handling(c, first)
    assert summary['historical_state'] == 'covered' and summary['state'] == 'awaiting'
    assert not summary['source_current'] and not summary['content_completed']
    page = inbox(c)
    assert page['items'][0]['message_id'] == first['id']
    assert page['items'][0]['task_request'] is None
    with pytest.raises(DevError):
        submit(c, raw)
    # A new explicit observation never re-attests the old owner-source envelope.
    observed = {**page['items'][0]['handling_arguments'], 'action': 'dot_handle',
                'handling_state': 'observed', 'reason_code': 'received'}
    submit(c, observed)
    assert inbox(c)['items'][0]['task_request'] is None
    assert len(c.s.store.all('SELECT * FROM dot_message_coverage')) == 1


def test_reply_revision_change_invalidates_coverage_evidence_read_only(collab):
    c = duplex(collab)
    first = source(c)
    reply = send_dot(c, 'Answer', first['id'])['message']
    submit(c, coverage(c, [first], reply))
    c.s.store.execute('UPDATE collaboration_messages SET version=version+1 WHERE id=?', (reply['id'],))
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    summary = handling(c, first)
    assert summary['state'] == 'awaiting' and not summary['evidence_current']
    assert inbox(c)['items'][0]['message_id'] == first['id']
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before


def test_replay_reauthorizes_current_binding_and_scope(collab):
    c = duplex(collab)
    first = source(c)
    raw = request(c, [first])
    submit(c, raw)
    other = replace(c.actor, grant_id='second', actor='mcp:second:fixture')
    with pytest.raises(DevError):
        submit(c, raw, other)
    with pytest.raises(DevError):
        submit(c, raw, c.owner)
    c.s.store.execute("UPDATE grants SET projects=? WHERE id='broad'", (canonical(['proj', 'otherproj']),))
    with pytest.raises(DevError) as exc:
        submit(c, raw)
    assert exc.value.code == 'DOT_HANDLING_SCOPE_CHANGED'
    assert handling(c, first)['state'] == 'awaiting'
    c.s.store.execute("UPDATE grants SET revoked=1 WHERE id='broad'")
    with pytest.raises(DevError):
        submit(c, raw)


def test_coverage_chain_is_rejected_even_if_corrupt_input_link_is_inserted(collab):
    c = duplex(collab)
    first = source(c)
    reply = send_dot(c, 'Answer', first['id'])['message']
    c.s.store.execute("""INSERT INTO collaboration_dot_messages
        (dot_id,message_id,message_version,message_digest,created) VALUES (?,?,?,?,?)""",
        (c.slot['id'], reply['id'], reply['version'], digest(reply['body_text']), c.clock[0]))
    with pytest.raises(DevError) as exc:
        submit(c, coverage(c, [first], reply))
    assert exc.value.code == 'DOT_HANDLING_COVERAGE_CHAIN'


def test_batch_bound_and_duplicates_are_explicit_errors_not_silent_truncation(collab):
    c = duplex(collab)
    first = source(c)
    rest = [source(c, str(i), first['id']) for i in range(20)]
    reply = send_dot(c, 'Combined answer', first['id'])['message']
    with pytest.raises(DevError):
        submit(c, coverage(c, [first, *rest], reply))
    assert not c.s.store.all('SELECT * FROM dot_message_coverage')
    duplicate = coverage(c, [first], reply)
    duplicate['inputs'].append(deepcopy(duplicate['inputs'][0]))
    with pytest.raises(DevError):
        submit(c, duplicate)
    result = submit(c, coverage(c, [first, *rest[:19]], reply))
    assert len(result['records']) == 20
    assert [item['message_id'] for item in inbox(c)['items']] == [rest[-1]['id']]


def test_same_text_does_not_infer_coverage_and_legacy_handled_is_preserved(collab):
    c = duplex(collab)
    first = source(c, 'Same text')
    second = source(c, 'Same text', first['id'])
    send_dot(c, 'Answer', first['id'])
    assert inbox(c)['items'][0]['message_id'] == second['id']
    call(c, {'tool': 'collaboration', 'arguments': {'action': 'dot_ack', **c.scope,
         'dot_id': c.slot['id'], 'message_id': second['id'], 'disposition': 'read', 'idempotency_key': key()}})
    call(c, {'tool': 'collaboration', 'arguments': {'action': 'dot_ack', **c.scope,
         'dot_id': c.slot['id'], 'message_id': second['id'], 'disposition': 'handled', 'idempotency_key': key()}})
    assert handling(c, second)['state'] == 'legacy_handled'
    assert not c.s.store.all('SELECT * FROM dot_message_coverage')


def test_current_source_visibility_is_checked_again_before_idempotent_replay(collab):
    c = duplex(collab)
    first = source(c)
    raw = request(c, [first])
    submit(c, raw)
    stored = c.s.object('collaboration_messages', c.room, first['id'])
    body = {**json.loads(stored['body']), 'provenance_project_ids': ['otherproj']}
    c.s.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (canonical(body), first['id']))
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    with pytest.raises(DevError):
        submit(c, raw)
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before


def test_handling_cannot_upgrade_task_only_consumer_to_duplex(collab):
    from tests.test_collaboration_dots import setup
    c = setup(collab)
    with pytest.raises(DevError) as exc:
        submit(c, {'action': 'dot_handle', **c.scope, 'dot_id': c.slot['id'],
            'handling_schema_version': 1, 'scope_digest': '0' * 64,
            'inputs': [{'message_id': 'missing', 'source_version': 1,
                'source_digest': '0' * 64, 'expected_revision': 0}],
            'handling_state': 'observed', 'reason_code': 'received', 'idempotency_key': key()})
    assert exc.value.code == 'DOT_CHAT_NOT_APPROVED'
    assert not c.s.store.all('SELECT * FROM dot_message_handling')


def test_summary_batch_is_one_query_without_new_records_and_has_no_cross_request_cache(collab, monkeypatch):
    c = duplex(collab)
    messages = [source(c, str(i)) for i in range(20)]
    original = c.s.store.all
    queries = []

    def tracked(sql, values=()):
        queries.append(sql)
        return original(sql, values)

    monkeypatch.setattr(c.s.store, 'all', tracked)
    summaries = c.s.message_handling.summaries([m['id'] for m in messages])
    assert len(summaries) == 20 and len(queries) == 1
    first = messages[0]
    submit(c, request(c, [first]))
    c.s.store.execute("UPDATE grants SET projects=? WHERE id='broad'", (canonical(['proj', 'otherproj']),))
    assert handling(c, first)['state'] == 'awaiting'


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['reply_committed', 'covered', 'no_response_required'])
async def test_connection_test_and_confirmation_do_not_reopen_completed_inputs(collab, decision):
    from tests.test_collaboration_dot_relay import subscribe

    c = duplex(collab)
    receiver, _ = await subscribe(c)  # In-memory synthetic receiver, never a real webhook.
    first = source(c, 'Thank you' if decision == 'no_response_required' else 'A question')
    if decision == 'no_response_required':
        submit(c, request(c, [first], decision, 'acknowledgement_only', confirms_no_new_intent=True))
    else:
        reply = send_dot(c, 'Final answer', first['id'])['message']
        if decision == 'covered':
            submit(c, coverage(c, [first], reply))
    before = handling(c, first)
    assert before['state'] == decision and before['content_completed']
    assert not inbox(c)['items']
    slot = c.s.joining.slot(c.room, c.slot['id'])
    initial_version = slot['version']
    c.s.joining.control({**c.scope, 'slot_id': slot['id'], 'expected_version': slot['version'],
                        'action': 'test', 'idempotency_key': key()}, c.owner)
    assert c.s.joining.slot(c.room, slot['id'])['version'] > initial_version
    assert not inbox(c)['items']
    assert handling(c, first) == before
    await c.s.events.tick()  # The original input is delivered first, preserving route order.
    await c.s.events.tick()  # Then the one queued synthetic connection test.
    current = c.s.joining.slot(c.room, slot['id'])
    routes = c.s.joining.routes(c.room, current)
    accepted = [route['test']['event_id'] for route in routes
                if route['test'] and route['test']['state'] == 'accepted']
    assert accepted and receiver.requests
    c.s.joining.control({**c.scope, 'slot_id': slot['id'], 'expected_version': current['version'],
        'action': 'confirm_chat', 'confirmed_received': True, 'test_event_ids': accepted,
        'idempotency_key': key()}, c.owner)
    assert not inbox(c)['items']
    assert handling(c, first) == before


def test_inbox_does_not_expose_now_hidden_reply_through_handling_or_existing_reply(collab):
    c = duplex(collab)
    first = source(c)
    reply = send_dot(c, 'A scoped question', first['id'], complete=False)['message']
    submit(c, request(c, [first], 'waiting_input', 'question_asked',
                     reply_id=reply['id'], reply_version=reply['version']))
    stored = c.s.object('collaboration_messages', c.room, reply['id'])
    body = {**json.loads(stored['body']), 'provenance_project_ids': ['otherproj']}
    c.s.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (canonical(body), reply['id']))
    assert not c.s.chatroom.message_visible(c.actor, c.s.object('collaboration_messages', c.room, reply['id']))
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    item = inbox(c)['items'][0]
    assert item['handling']['reply_id'] == '' and item['handling']['reply_version'] == 0
    assert item['handling']['state'] == 'awaiting' and not item['handling']['evidence_current']
    assert item['existing_reply'] is None
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before


def test_summary_without_principal_never_defaults_to_related_reference_access(collab):
    c = duplex(collab)
    first = source(c)
    send_dot(c, 'A final answer', first['id'])
    public = c.s.message_handling.summaries([first['id']])[(c.slot['id'], first['id'])]
    assert public['reply_id'] == '' and public['reply_version'] == 0 and public['actor_ref'] == ''
    assert not public['evidence_current'] and not public['content_completed']
    assert handling(c, first)['content_completed']


def test_work_reference_and_replay_use_current_goal_authorization(collab, monkeypatch):
    c = duplex(collab)
    first = source(c, 'Run approved checks')
    call(c, inbox(c)['items'][0]['task_request'])
    work = c.s.store.one('SELECT * FROM coordination_work')
    raw = request(c, [first], 'work_linked', 'existing_work', work_item_id=work['id'])
    submit(c, raw)
    original = c.s.coordination.authorize

    def deny_related_goal(principal, room, goal, **options):
        if goal['id'] == work['goal_id']:
            raise DevError('GOAL_PARTICIPANT_REQUIRED', 'Current goal access was removed', 403)
        return original(principal, room, goal, **options)

    monkeypatch.setattr(c.s.coordination, 'authorize', deny_related_goal)
    summary = handling(c, first)
    assert summary['work_item_id'] == '' and summary['work_state'] is None
    assert not summary['evidence_current'] and not summary['content_completed']
    # Authorization must precede replay of a saved record containing the work ID.
    with pytest.raises(DevError) as exc:
        submit(c, raw)
    assert exc.value.code == 'GOAL_PARTICIPANT_REQUIRED'
    assert c.s.store.one('SELECT * FROM coordination_work') == work


def test_revised_waiting_explanation_invalidates_old_version_evidence(collab):
    c = duplex(collab)
    first = source(c)
    reply = send_dot(c, 'Need a decision', first['id'], complete=False)['message']
    submit(c, request(c, [first], 'waiting_input', 'question_asked',
                     reply_id=reply['id'], reply_version=reply['version']))
    c.s.invoke('collaboration', {'action': 'dot_update', **c.scope, 'dot_id': c.slot['id'],
        'message_id': reply['id'], 'expected_version': reply['version'], 'body_text': 'Updated situation',
        'complete': False, 'idempotency_key': key()}, c.actor)
    assert handling(c, first)['state'] == 'awaiting'
    assert not handling(c, first)['evidence_current']
