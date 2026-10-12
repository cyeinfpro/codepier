"""Independent authority boundary regressions; all state/provider responses are synthetic."""
import asyncio
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import inbox
from tests.test_collaboration_dots import call, lease
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet
from tests.test_dot_weixin_owner_authority import approve, fresh, task


def revoke(c, w, binding, scope):
    current = w.binding(binding['binding_id'], c.owner, c.room)
    auth = c.s.channel_owner.current(current['id'])
    return c.s.channel_owner.control({
        **scope, 'binding_id': current['id'],
        'expected_binding_version': current['version'],
        'expected_authority_version': auth['version'],
        'action': 'revoke', 'idempotency_key': key(),
    }, c.owner)


def interim(c, source):
    posted = c.s.dot_chat.message({
        **c.scope, 'dot_id': c.slot['id'], 'reply_to_id': source,
        'body_text': 'Synthetic progress', 'complete': False,
        'idempotency_key': key(),
    }, c.actor)
    delivery = c.s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?',
                            (posted['message']['id'],))
    assert delivery
    return c.s.store.one('SELECT * FROM weixin_parts WHERE delivery_id=?',
                        (delivery['id'],))


def revise(c, w, account, source, *, delete=False):
    message = c.s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (source,))
    origin = json.loads(message['body'])['external_origin']
    created = int(origin['provider_created'])
    stamp = {'delete_time_ms' if delete else 'update_time_ms': created + 1000}
    return ingest(c, w, account, packet(int(origin['external_message_id']),
        'Changed source', create_time_ms=created, **stamp))


@pytest.mark.parametrize('idle_days', [0, 400])
def test_fresh_owner_task_runs_complete_bounded_managed_lifecycle(collab, idle_days):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, budget_mode='rolling_24h', confirm_daily_budget=True)
    before_grants = c.s.store.all('SELECT * FROM grants ORDER BY id')
    c.clock[0] += idle_days * 86400
    source = fresh(c, w, account)
    created = task(c, source)
    detail = call(c, created['read_request'])
    assert detail['trusted_author']['kind'] == 'verified_external_owner'
    assert detail['source_message']['origin'] == 'external_channel'
    page = call(c, inbox(c)['work_inbox_request'])
    pending = next(item for item in page['items'] if item['category'] == 'claimable')
    work = call(c, pending['claim_request'])['work_item']
    goal = c.s.store.one('SELECT * FROM coordination_goals WHERE id=?', (work['goal_id'],))
    assert 0 < goal['expires_at'] - c.clock[0] <= 3600
    assert 0 < work['lease_until'] - c.clock[0] <= 3600
    c.s.coordination.progress({**lease(c, work), 'summary': 'Synthetic progress'}, c.actor)
    command = {**lease(c, work), 'tool': 'exec',
               'arguments': {'command': 'printf synthetic-authority-review'}}
    admitted = c.s.coordination.admit(command, c.actor)
    assert c.s.coordination.admit(command, c.actor)['operation_id'] == admitted['operation_id']
    operation = c.s.store.one('SELECT * FROM operations WHERE id=?', (admitted['operation_id'],))
    assert operation['project_id'] == 'proj' and operation['grant_id'] == c.actor.grant_id
    # An isolated synthetic executor receipt, not a real shell/provider execution.
    c.s.runtime.complete(operation, {'ok': True, 'data': {
        'exit_code': 0, 'output': 'synthetic-authority-review'}})
    result = {**lease(c, work), 'outcome': 'succeeded', 'summary': 'Verified synthetic receipt',
              'operation_ids': [admitted['operation_id']]}
    finished = c.s.coordination.result(result, c.actor)['work_item']
    assert finished['state'] == 'succeeded'
    assert finished['result']['execution_verified']
    assert finished['result']['provenance_project_ids'] == ['proj']
    assert c.s.coordination.result(result, c.actor)['work_item']['state'] == 'succeeded'
    assert len(c.s.store.all('SELECT * FROM operations')) == 1
    deliveries = c.s.store.all('SELECT * FROM weixin_deliveries ORDER BY created')
    assert len(deliveries) == 3
    for delivery in deliveries:
        assert delivery['source_message_id'] == source
        w.delivery.validate(delivery)
    assert c.s.store.all('SELECT * FROM grants ORDER BY id') == before_grants


@pytest.mark.parametrize('change', ['revoke', 'edit', 'delete'])
def test_source_revocation_blocks_new_steps_and_late_completed_result(collab, change):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    source = fresh(c, w, account)
    task(c, source)
    page = call(c, inbox(c)['work_inbox_request'])
    work = call(c, next(item for item in page['items']
                       if item['category'] == 'claimable')['claim_request'])['work_item']
    admitted = c.s.coordination.admit({**lease(c, work), 'tool': 'exec',
        'arguments': {'command': 'printf synthetic-authority-review'}}, c.actor)
    operation = c.s.store.one('SELECT * FROM operations WHERE id=?', (admitted['operation_id'],))
    c.s.runtime.complete(operation, {'ok': True, 'data': {'exit_code': 0, 'output': 'synthetic'}})
    if change == 'revoke':
        revoke(c, w, binding, scope)
    else:
        revise(c, w, account, source, delete=change == 'delete')
    with pytest.raises(DevError):
        c.s.coordination.admit({**lease(c, work), 'tool': 'exec',
            'arguments': {'command': 'printf forbidden-new-step'}}, c.actor)
    with pytest.raises(DevError, match='来源身份'):
        c.s.coordination.result({**lease(c, work), 'outcome': 'succeeded',
            'summary': 'Must not publish', 'operation_ids': [admitted['operation_id']]}, c.actor)
    assert len(c.s.store.all('SELECT * FROM operations')) == 1
    assert not c.s.store.all("SELECT * FROM collaboration_messages WHERE kind='delegation_result'")


@pytest.mark.parametrize('change', ['revoke', 'edit', 'delete'])
def test_permanently_invalid_unsent_reply_does_not_block_fresh_authority(collab, change):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    source = fresh(c, w, account)
    old = interim(c, source)
    if change == 'revoke':
        revoke(c, w, binding, scope)
        approve(c, w, binding, scope)
    else:
        revise(c, w, account, source, delete=change == 'delete')
    new = interim(c, fresh(c, w, account, 10))
    w.client_factory = lambda *args: pytest.fail('Invalid source must not reach provider')
    asyncio.run(w.delivery.send_once(old['id']))
    old_row = c.s.store.one('SELECT * FROM weixin_parts WHERE id=?', (old['id'],))
    assert old_row['state'] in {'cancelled', 'rejected'}
    assert old_row['attempts'] == 0
    assert w.delivery.claim(new['id']) is not None


def test_revoke_fences_preparing_reply_before_any_send(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    old = interim(c, fresh(c, w, account))
    claim = w.delivery.claim(old['id'])
    assert claim is not None
    revoke(c, w, binding, scope)
    with pytest.raises(DevError):
        w.delivery.mark_sending(claim, [{'type': 1, 'text_item': {'text': 'Do not send'}}])
    row = c.s.store.one('SELECT * FROM weixin_parts WHERE id=?', (old['id'],))
    assert row['state'] == 'cancelled' and row['attempts'] == 0
    assert row['lease_fence'] > claim['lease_fence']


def test_unknown_send_is_not_replayed_or_skipped_after_new_consent(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    old = interim(c, fresh(c, w, account))
    calls = []
    class Provider:
        async def send(self, peer, context, client_id, items):
            calls.append(client_id)
            raise TimeoutError('Provider may have accepted this synthetic send')
    w.client_factory = lambda *args: Provider()
    asyncio.run(w.delivery.send_once(old['id']))
    assert c.s.store.one('SELECT state FROM weixin_parts WHERE id=?',
                         (old['id'],))['state'] == 'unknown'
    revoke(c, w, binding, scope)
    approve(c, w, binding, scope)
    new = interim(c, fresh(c, w, account, 10))
    c.clock[0] += 200
    w.delivery.reconcile()
    asyncio.run(w.delivery.send_once(old['id']))
    assert w.delivery.claim(new['id']) is None
    assert calls == [old['client_id']]
    assert c.s.store.one('SELECT state FROM weixin_parts WHERE id=?',
                         (old['id'],))['state'] == 'unknown'


def test_pause_refresh_pairing_cannot_substitute_owner_peer(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    fresh(c, w, account)
    current = w.binding(binding['binding_id'], c.owner, c.room)
    w.control({**scope, 'binding_id': current['id'], 'expected_version': current['version'],
               'action': 'pause', 'idempotency_key': key()}, c.owner)
    current = w.binding(binding['binding_id'], c.owner, c.room)
    refreshed = w.control({**scope, 'binding_id': current['id'],
        'expected_version': current['version'], 'action': 'refresh_pairing',
        'idempotency_key': key()}, c.owner)
    ingest(c, w, account, packet(10, refreshed['pairing_challenge'], from_user_id='different-peer'))
    current = w.binding(binding['binding_id'], c.owner, c.room)
    with pytest.raises(DevError) as caught:
        w.control({**scope, 'binding_id': current['id'], 'expected_version': current['version'],
            'action': 'activate', 'confirm_peer_id': 'different-peer',
            'idempotency_key': key()}, c.owner)
    assert caught.value.code == 'CHANNEL_OWNER_PEER_CHANGED'
    assert w.binding(binding['binding_id'], c.owner, c.room)['peer_id'] == 'synthetic-peer'


def test_daily_budget_has_atomic_hundred_request_cap_and_never_resets_audit(collab):
    from concurrent.futures import ThreadPoolExecutor

    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, budget_mode='rolling_24h', confirm_daily_budget=True)
    for identifier in range(2, 101):
        task(c, fresh(c, w, account, identifier))
    policy = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    assert policy['uses'] == 99
    sources = [fresh(c, w, account, identifier) for identifier in (101, 102)]
    requests = [next(item['task_request'] for item in inbox(c)['items']
                     if item['message_id'] == source) for source in sources]
    def attempt(request):
        try:
            return call(c, request)
        except DevError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, requests))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert outcomes.count('DELEGATION_BUDGET_EXCEEDED') == 1
    policy = c.s.delegation.policy(c.room, policy['id'])
    assert policy['uses'] == 100 and c.s.channel_owner.budget_used(policy) == 100
    assert len(c.s.store.all('SELECT * FROM delegation_requests')) == 100
    c.clock[0] += 86401
    task(c, fresh(c, w, account, 103))
    policy = c.s.delegation.policy(c.room, policy['id'])
    assert policy['uses'] == 101 and c.s.channel_owner.budget_used(policy) == 1
    assert len(c.s.store.all('SELECT * FROM delegation_requests')) == 101


@pytest.mark.parametrize('delete', [False, True])
def test_source_revision_during_preparation_terminates_unsent_part(collab, delete):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    source = fresh(c, w, account)
    old = interim(c, source)
    claim = w.delivery.claim(old['id'])
    assert claim is not None
    revise(c, w, account, source, delete=delete)
    with pytest.raises(DevError) as caught:
        w.delivery.mark_sending(claim, [{'type': 1, 'text_item': {'text': 'Do not send'}}])
    assert caught.value.code == 'CHANNEL_OWNER_SOURCE_SUPERSEDED'
    w.delivery.finish(claim, None, caught.value)
    row = c.s.store.one('SELECT * FROM weixin_parts WHERE id=?', (old['id'],))
    assert row['state'] in {'cancelled', 'rejected'} and row['attempts'] == 0
    new = interim(c, fresh(c, w, account, 10))
    assert w.delivery.claim(new['id']) is not None


@pytest.mark.parametrize('initial_state', ['queued', 'preparing', 'attention_unsent', 'unknown'])
def test_same_peer_renewal_unblocks_unsent_lane_but_preserves_unknown(collab, initial_state):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    old = interim(c, fresh(c, w, account))
    calls = []
    class Provider:
        async def send(self, peer, context, client_id, items):
            calls.append(client_id)
            raise TimeoutError('Provider may have accepted this synthetic send')
    if initial_state == 'preparing':
        assert w.delivery.claim(old['id']) is not None
    elif initial_state == 'attention_unsent':
        claim = w.delivery.claim(old['id'])
        w.delivery.finish(claim, None, DevError('WEIXIN_SEND_INTERRUPTED',
            'Synthetic interruption before any send', 503))
    elif initial_state == 'unknown':
        w.client_factory = lambda *args: Provider()
        asyncio.run(w.delivery.send_once(old['id']))
    renewal = w.qr.renewal_intent({
        **scope, 'renew_account_id': account['account_id'],
        'expected_credential_version': account['credential_version'],
    }, c.room, c.owner)
    # Exercise the exact internal rotation used by accepted QR enrollment;
    # all credentials and provider packets here belong to the isolated fixture.
    w.import_account({
        **scope, 'bot_id': account['bot_id'], 'label': 'Synthetic renewal',
        'token': 'synthetic-renewed-secret', 'expected_version': account['credential_version'],
        'confirm_credential_storage': True, 'confirm_provider_terms': True,
        'idempotency_key': key(),
    }, c.owner, renewal=renewal)
    assert c.s.channel_owner.current(binding['binding_id'])['state'] == 'pending_revalidation'
    challenge = next(item for item in w.listing(scope, c.owner)['bindings']
                     if item['id'] == binding['binding_id'])['pairing_challenge']
    ingest(c, w, account, packet(100, challenge))
    current = w.binding(binding['binding_id'], c.owner, c.room)
    w.control({
        **scope, 'binding_id': current['id'], 'expected_version': current['version'],
        'action': 'activate', 'confirm_peer_id': 'synthetic-peer', 'idempotency_key': key(),
    }, c.owner)
    assert c.s.channel_owner.current(binding['binding_id'])['state'] == 'active'
    new = interim(c, fresh(c, w, account, 101))
    current_old = c.s.store.one('SELECT * FROM weixin_parts WHERE id=?', (old['id'],))
    if initial_state == 'unknown':
        assert current_old['state'] == 'unknown'
        assert w.delivery.claim(new['id']) is None
        asyncio.run(w.delivery.send_once(old['id']))
        assert calls == [old['client_id']]
    else:
        assert current_old['state'] in {'cancelled', 'rejected'}
        assert current_old['attempts'] == 0
        assert w.delivery.claim(new['id']) is not None
