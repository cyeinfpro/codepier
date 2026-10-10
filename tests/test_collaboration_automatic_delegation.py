"""Automatic tasks use prior owner consent and real records, never fabricated IDs."""
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from shared.collaboration_contracts import DelegationPolicySet, MessageCreate
from shared.util import DevError
from tests.collaboration_support import collab, key  # noqa: F401
from tests.test_collaboration_delegation_consumer import setup, request, connection, inbox


def automatic(c, **changes):
    raw = request(c)
    raw.pop('delegation')
    return {**raw, 'dispatch_mode': 'automatic', 'automatic_policy_version': c.policy['version'], **changes}


def discover(c, actor=None, **extra):
    return c.s.invoke('collaboration_query', {'action': 'delegations', **c.scope,
        'conversation_id': c.room['id'], **extra}, actor or c.actor)


def test_automatic_task_creates_real_id_and_returns_exact_fresh_read_request(collab):
    c = setup(collab, automatic_delegation=True, capabilities=['read'])
    saved = connection(c)
    raw = automatic(c)
    result = c.s.chatroom.create(raw, c.owner)
    identifier = result['delegation_id']
    assert len(identifier) == 32 and result['scheduled']
    assert result['message']['body']['delegation']['automatic']
    assert result['read_request']['arguments']['delegation_id'] == identifier
    fresh = c.s.invoke(result['read_request']['tool'], result['read_request']['arguments'], c.actor)
    assert fresh['delegation']['id'] == identifier
    assert fresh['trusted_author']['authenticated'] and fresh['trusted_author']['kind'] == 'panel_owner'
    assert fresh['source_message']['body']['body_text'] == raw['body_text']
    assert fresh['request_scope'] == {'execution_targets': ['project_agent'], 'capabilities': ['read']}
    assert inbox(c, saved)['items'][0]['delegation_id'] == identifier
    assert not c.s.store.all('SELECT * FROM operations')
    assert all(row['attempt'] == 0 for row in c.s.store.all('SELECT * FROM coordination_work'))


def test_retries_return_original_id_even_after_policy_is_paused(collab):
    c = setup(collab, automatic_delegation=True)
    raw = automatic(c)
    first = c.s.chatroom.create(raw, c.owner)
    assert c.s.chatroom.create(raw, c.owner)['delegation_id'] == first['delegation_id']
    c.s.delegation.control({**c.scope, 'policy_id': c.policy['id'], 'expected_version': c.policy['version'],
        'action': 'pause', 'idempotency_key': key()}, c.owner)
    replay = c.s.chatroom.create({**raw, 'idempotency_key': key()}, c.owner)
    assert replay['delegation_id'] == first['delegation_id']
    assert len(c.s.store.all('SELECT * FROM delegation_requests')) == 1
    assert len(c.s.store.all('SELECT * FROM coordination_goals')) == 1
    assert c.s.store.one('SELECT uses FROM delegation_policies WHERE id=?', (c.policy['id'],))['uses'] == 1
    with pytest.raises(DevError, match='已暂停'):
        c.s.chatroom.create(automatic(c), c.owner)


@pytest.mark.parametrize('mode', [None, 'discussion'])
def test_discussion_stays_inert_even_with_automatic_rule(collab, mode):
    c = setup(collab, automatic_delegation=True)
    raw = automatic(c)
    raw.pop('automatic_policy_version')
    raw.pop('dispatch_mode')
    if mode:
        raw['dispatch_mode'] = mode
    result = c.s.chatroom.create(raw, c.owner)
    assert not result['scheduled'] and not result.get('delegation_id')
    assert not c.s.store.all('SELECT * FROM delegation_requests')
    assert not c.s.store.all('SELECT * FROM coordination_goals')


@pytest.mark.parametrize('mutation', ['disabled', 'stale', 'expired', 'revoked', 'connector'])
def test_invalid_automatic_authority_does_not_save_task_or_execute(collab, mutation):
    c = setup(collab, automatic_delegation=mutation != 'disabled')
    raw = automatic(c)
    actor = c.owner
    if mutation == 'stale':
        raw['automatic_policy_version'] += 1
    elif mutation == 'expired':
        c.clock[0] = c.policy['expires_at'] + 1
    elif mutation == 'revoked':
        c.s.store.execute("UPDATE grants SET revoked=1 WHERE id='broad'")
    elif mutation == 'connector':
        actor = c.actor
    with pytest.raises(DevError):
        c.s.chatroom.create(raw, actor)
    assert not c.s.store.all('SELECT * FROM delegation_requests')
    assert not c.s.store.all('SELECT * FROM coordination_goals')
    assert not c.s.store.all('SELECT * FROM collaboration_messages')
    assert not c.s.store.all('SELECT * FROM operations')


def test_reply_multiple_recipients_and_manual_payload_cannot_be_automatic(collab):
    c = setup(collab, automatic_delegation=True)
    raw = automatic(c)
    invalid = [
        {'automatic_policy_version': 0},
        {'reply_to_id': 'any-reply'}, {'mentions': []},
        {'mentions': [*raw['mentions'], {'slot_id': 'another-slot'}]},
        {'delegation': request(c)['delegation']},
    ]
    for change in invalid:
        with pytest.raises(ValidationError):
            MessageCreate.model_validate({**raw, **change})
    assert not c.s.store.all('SELECT * FROM delegation_requests')


def test_auto_policy_requires_one_target_and_rejects_string_truthiness(collab):
    c = setup(collab)
    for change in [
        {'automatic_delegation': 'true'},
        {'automatic_delegation': True, 'automatic_acceptance': '   '},
        {'automatic_delegation': True, 'execution_targets': ['project_agent', 'vps:fixture']},
    ]:
        with pytest.raises(ValidationError):
            DelegationPolicySet.model_validate({**c.policy_raw, **change})


def test_discovery_is_read_only_paged_and_does_not_leak_another_connections_ids(collab):
    c = setup(collab, automatic_delegation=True)
    identifiers = {c.s.chatroom.create(automatic(c), c.owner)['delegation_id'] for _ in range(3)}
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    first = discover(c, limit=1)
    assert first['discovery_only'] and first['grants_authority'] is False and first['next_cursor']
    page, found = first, set()
    while True:
        found.update(item['delegation_id'] for item in page['items'])
        if not page['next_cursor']:
            break
        page = discover(c, limit=1, cursor=page['next_cursor'])
    assert found == identifiers
    second = replace(c.actor, grant_id='second', actor='mcp:second:fixture')
    assert discover(c, actor=second)['items'] == []
    with pytest.raises(DevError):
        discover(c, actor=second, cursor=first['next_cursor'])
    with pytest.raises(DevError):
        discover(c, cursor=first['next_cursor'] + 'x')
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert not c.s.store.all('SELECT * FROM operations')
    assert all(row['attempt'] == 0 for row in c.s.store.all('SELECT * FROM coordination_work'))


def test_old_manual_policies_never_gain_automatic_consent(collab):
    c = setup(collab)
    row = c.s.store.one('SELECT * FROM delegation_policies WHERE id=?', (c.policy['id'],))
    spec = json.loads(row['spec'])
    spec.pop('automatic_delegation')
    spec.pop('automatic_acceptance')
    c.s.store.execute('UPDATE delegation_policies SET spec=? WHERE id=?', (json.dumps(spec), c.policy['id']))
    assert c.s.delegation.view(c.s.delegation.policy(c.room, c.policy['id']), c.actor, c.room)['automatic_delegation'] is False
    with pytest.raises(DevError) as caught:
        c.s.chatroom.create(automatic(c), c.owner)
    assert caught.value.code == 'AUTOMATIC_DELEGATION_NOT_CONFIGURED'
    assert not c.s.store.all('SELECT * FROM delegation_requests')


def test_disabled_defaults_preserve_legacy_policy_save_fingerprint(collab, monkeypatch):
    s = collab[0]
    original, captured = s.mutation, []
    def mutation(principal, room, action, args, save):
        if action.startswith('delegation_policy_set:'):
            captured.append(dict(args))
        return original(principal, room, action, args, save)
    monkeypatch.setattr(s, 'mutation', mutation)
    setup(collab)
    assert len(captured) == 1
    assert 'automatic_delegation' not in captured[0] and 'automatic_acceptance' not in captured[0]
    setup(collab, automatic_delegation=True)
    assert captured[1]['automatic_delegation'] is True and captured[1]['automatic_acceptance']
