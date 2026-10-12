"""Explicit owner task authority never comes from a peer name or a pasted claim."""
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import inbox, no_work
from tests.test_collaboration_dots import call
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet


def approve(c, w, binding, scope, **extra):
    current = w.binding(binding['binding_id'], c.owner, c.room)
    option = c.s.channel_owner.options(current)
    old = c.s.channel_owner.current(current['id'])
    raw = {**scope, 'binding_id': current['id'], 'expected_binding_version': current['version'],
           'expected_authority_version': old['version'] if old else 0, 'action': 'approve',
           'confirm_peer_id': current['peer_id'], 'confirm_owner_tasks': True,
           'confirm_until_revoked': True, 'expected_scope_digest': option['scope_digest'],
           'idempotency_key': key(), **extra}
    return c.s.channel_owner.control(raw, c.owner), raw


def fresh(c, w, account, identifier=2, **extra):
    return ingest(c, w, account, packet(identifier, '检查 README 并回复真实结果',
        create_time_ms=int(c.clock[0] * 1000) + 1, **extra))['message_ids'][0]


def task(c, source):
    item = next(i for i in inbox(c)['items'] if i['message_id'] == source)
    assert item['trusted_author']['kind'] == 'verified_external_owner'
    assert item['trusted_author']['authenticated']
    assert item['task_request']
    return call(c, item['task_request'])


def test_explicit_approval_only_changes_channel_scope_and_fresh_owner_can_task(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    before_grants = c.s.store.all('SELECT * FROM grants ORDER BY id')
    before_tokens = c.s.store.all('SELECT * FROM tokens ORDER BY id')
    old = ingest(c, w, account)['message_ids'][0]
    approved, raw = approve(c, w, binding, scope)
    assert approved['owner_authorization']['state'] == 'active'
    assert c.s.channel_owner.control(raw, c.owner) == approved
    assert c.s.store.all('SELECT * FROM grants ORDER BY id') == before_grants
    assert c.s.store.all('SELECT * FROM tokens ORDER BY id') == before_tokens
    assert next(i for i in inbox(c)['items'] if i['message_id'] == old)['task_request'] is None
    no_work(c)
    source = fresh(c, w, account, 3)
    request = next(i for i in inbox(c)['items'] if i['message_id'] == source)['task_request']
    result = call(c, request)
    detail = call(c, result['read_request'])
    assert detail['trusted_author']['kind'] == 'verified_external_owner'
    assert detail['source_message']['origin'] == 'external_channel'
    assert detail['request_scope']['capabilities'] == ['read', 'write', 'execute']
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 1
    assert call(c, request)['delegation_id'] == result['delegation_id']


@pytest.mark.parametrize('change', [
    {'confirm_owner_tasks': False}, {'confirm_until_revoked': False},
    {'confirm_peer_id': 'another-peer'}, {'confirm_peer_id': '陌生联系人'}, {'expected_scope_digest': 'f' * 64},
    {'budget_mode': 'rolling_24h', 'confirm_daily_budget': False},
    {'budget_mode': 'unlimited'},
    {'budget_mode': 'unlimited', 'confirm_unlimited_budget': False},
    {'budget_mode': 'unlimited', 'confirm_daily_budget': True},
])
def test_exact_one_time_consent_is_required(collab, change):
    c, w, account, binding, scope = fixture_channel(collab)
    with pytest.raises(DevError):
        approve(c, w, binding, scope, **change)
    assert not c.s.channel_owner.current(binding['binding_id'])
    no_work(c)


def test_connector_cannot_create_owner_authority(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    current = w.binding(binding['binding_id'], c.owner, c.room)
    raw = {**scope, 'binding_id': current['id'], 'expected_binding_version': current['version'],
           'action': 'approve', 'confirm_peer_id': current['peer_id'], 'confirm_owner_tasks': True,
           'confirm_until_revoked': True, 'expected_scope_digest': c.s.channel_owner.options(current)['scope_digest'],
           'idempotency_key': key()}
    with pytest.raises(DevError):
        c.s.channel_owner.control(raw, c.actor)
    no_work(c)


def test_until_revoked_survives_idle_400_days_but_lease_stays_bounded(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, budget_mode='rolling_24h', confirm_daily_budget=True)
    c.clock[0] += 400 * 86400
    assert w.valid(binding['binding_id'])[0]['lifetime_mode'] == 'until_revoked'
    source = fresh(c, w, account)
    result = task(c, source)
    link = c.s.store.one('SELECT * FROM delegation_requests WHERE id=?', (result['delegation_id'],))
    goal = c.s.store.one('SELECT * FROM coordination_goals WHERE id=?', (link['goal_id'],))
    assert 0 < goal['expires_at'] - c.clock[0] <= 3600
    assert c.s.store.one('SELECT uses FROM delegation_policies WHERE id=?', (link['policy_id'],))['uses'] == 1


@pytest.mark.parametrize('change', ['binding_pause', 'binding_revoke', 'authority_revoke', 'grant_revoke', 'project_move'])
@pytest.mark.parametrize('unlimited', [False, True])
def test_revocation_or_scope_change_stops_existing_source_and_pending_task(collab, change, unlimited):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, **({'budget_mode': 'unlimited', 'confirm_unlimited_budget': True} if unlimited else {}))
    source = fresh(c, w, account)
    result = task(c, source)
    current = w.binding(binding['binding_id'], c.owner, c.room)
    if change.startswith('binding_'):
        w.control({**scope, 'binding_id': current['id'], 'expected_version': current['version'],
                   'action': change.removeprefix('binding_'), 'idempotency_key': key()}, c.owner)
    elif change == 'authority_revoke':
        auth = c.s.channel_owner.current(current['id'])
        c.s.channel_owner.control({**scope, 'binding_id': current['id'], 'expected_binding_version': current['version'],
            'expected_authority_version': auth['version'], 'action': 'revoke', 'idempotency_key': key()}, c.owner)
    elif change == 'grant_revoke':
        c.s.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (c.actor.grant_id,))
    else:
        c.s.store.execute("UPDATE projects SET root='/another-root' WHERE id='proj'")
    with pytest.raises(DevError):
        call(c, result['read_request'])
    stored = c.s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (source,))
    assert not c.s.channel_owner.author(stored)['authenticated']


@pytest.mark.parametrize('mode', ['missing_time', 'before_approval', 'revised', 'deleted', 'other_peer', 'group'])
def test_history_and_unverified_sources_do_not_acquire_execution_authority(collab, mode):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    changes = {'create_time_ms': int(c.clock[0] * 1000) + 1}
    if mode == 'missing_time':
        changes = {}
    elif mode == 'before_approval':
        changes['create_time_ms'] -= 100000
    elif mode == 'deleted':
        changes['delete_time_ms'] = changes['create_time_ms'] + 1
    elif mode == 'other_peer':
        changes['from_user_id'] = 'stranger'
    elif mode == 'group':
        changes['group_id'] = 'group'
    elif mode == 'revised':
        fresh(c, w, account)
        changes['create_time_ms'] = int((c.clock[0] - 3) * 1000) + 1
        changes['update_time_ms'] = changes['create_time_ms'] + 10
    result = ingest(c, w, account, packet(2, '执行正文', **changes))
    if result['message_ids']:
        source = result['message_ids'][0]
        stored = c.s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (source,))
        assert not c.s.channel_owner.author(stored)['authenticated']
    no_work(c)


def test_rolling_budget_is_explicit_persistent_and_does_not_reset_audit_uses(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, budget_mode='rolling_24h', confirm_daily_budget=True)
    source = fresh(c, w, account)
    task(c, source)
    policy = c.s.delegation.policy(c.room, c.s.dots.find(c.slot['id'])['policy_id'])
    c.s.store.execute('UPDATE delegation_policies SET uses=100 WHERE id=?', (policy['id'],))
    policy = c.s.delegation.policy(c.room, policy['id'])
    assert c.s.channel_owner.budget_used(policy) == 1
    c.clock[0] += 86401
    assert c.s.channel_owner.budget_used(policy) == 0
    assert c.s.delegation.policy(c.room, policy['id'])['uses'] == 100


def test_legacy_total_budget_still_exhausts_and_does_not_silently_reset(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    policy = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    c.s.store.execute('UPDATE delegation_policies SET uses=100 WHERE id=?', (policy['id'],))
    c.clock[0] += 86401
    source = fresh(c, w, account)
    with pytest.raises(DevError) as caught:
        task(c, source)
    assert caught.value.code == 'DELEGATION_BUDGET_EXCEEDED'


def test_interim_reply_is_relayed_only_for_verified_owner_and_revocation_fences_it(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    source = fresh(c, w, account)
    posted = c.s.dot_chat.message({**c.scope, 'dot_id': c.slot['id'], 'reply_to_id': source,
        'body_text': '正在检查', 'complete': False, 'idempotency_key': key()}, c.actor)
    delivery = c.s.store.one('SELECT * FROM weixin_deliveries WHERE message_id=?', (posted['message']['id'],))
    assert delivery
    current = w.binding(binding['binding_id'], c.owner, c.room)
    auth = c.s.channel_owner.current(current['id'])
    c.s.channel_owner.control({**scope, 'binding_id': current['id'], 'expected_binding_version': current['version'],
        'expected_authority_version': auth['version'], 'action': 'revoke', 'idempotency_key': key()}, c.owner)
    with pytest.raises(DevError):
        w.delivery.validate(delivery)


def test_legacy_mutation_payloads_keep_upgrade_idempotency_fingerprints():
    from hub.collaboration.weixin_schema import Bind, QRAccept, QRStart
    base = {'project': 'proj', 'idempotency_key': 'legacy-request'}
    bind = Bind.model_validate({**base, 'account_id': 'account', 'dot_id': 'dot',
                               'confirm_external_data_ingress': True}).model_dump()
    assert 'lifetime_mode' not in bind and 'confirm_until_revoked' not in bind
    qr = QRStart.model_validate({**base, 'confirm_credential_storage': True,
                                  'confirm_provider_terms': True}).model_dump()
    assert 'renew_account_id' not in qr and 'expected_credential_version' not in qr
    accept = QRAccept.model_validate({**base, 'enrollment_id': 'enroll', 'expected_version': 1,
        'label': 'bot', 'confirm_scanner_id': 'scanner', 'confirm_credential_storage': True,
        'confirm_provider_terms': True}).model_dump()
    assert 'confirm_replace_existing' not in accept
    permanent = Bind.model_validate({**base, 'account_id': 'account', 'dot_id': 'dot',
        'confirm_external_data_ingress': True, 'lifetime_mode': 'until_revoked',
        'confirm_until_revoked': True}).model_dump()
    assert permanent['lifetime_mode'] == 'until_revoked' and permanent['confirm_until_revoked']


def test_task_approval_cannot_silently_enable_missing_reply_permission(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    current = w.binding(binding['binding_id'], c.owner, c.room)
    old_options = c.s.channel_owner.options(current)
    c.s.store.execute('UPDATE weixin_bindings SET allow_replies=0 WHERE id=?', (current['id'],))
    before = c.s.store.one('SELECT * FROM weixin_bindings WHERE id=?', (current['id'],))
    grants = c.s.store.all('SELECT * FROM grants ORDER BY id')
    tokens = c.s.store.all('SELECT * FROM tokens ORDER BY id')
    options = c.s.channel_owner.options(before)
    assert options['available'] is False and options['reason_code'] == 'CHANNEL_OWNER_REPLIES_REQUIRED'
    with pytest.raises(DevError) as exc:
        c.s.channel_owner.control({**scope, 'binding_id': current['id'],
            'expected_binding_version': current['version'], 'action': 'approve',
            'confirm_peer_id': current['peer_id'], 'confirm_owner_tasks': True,
            'confirm_until_revoked': True, 'expected_scope_digest': old_options['scope_digest'],
            'idempotency_key': key()}, c.owner)
    assert exc.value.code == 'CHANNEL_OWNER_REPLIES_REQUIRED'
    assert not c.s.channel_owner.current(current['id'])
    assert c.s.store.one('SELECT * FROM weixin_bindings WHERE id=?', (current['id'],)) == before
    assert c.s.store.all('SELECT * FROM grants ORDER BY id') == grants
    assert c.s.store.all('SELECT * FROM tokens ORDER BY id') == tokens


def test_long_lifetime_keeps_host_renewal_explicit_and_checkpoint_stable(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, budget_mode='rolling_24h', confirm_daily_budget=True)
    dot = c.s.dots.find(c.slot['id'])
    before = dot['checkpoint']
    config = c.s.dot_chat.configuration(c.room, dot, c.actor)
    assert config['subscription_lifecycle'] == {
        'renewal_managed_by': 'host', 'renew_before': 'refreshBefore', 'same_scope_only': True,
        'preserve_checkpoint': True, 'server_renews_host_grant': False}
    assert 'refreshBefore' in config['wake_instructions']
    c.clock[0] += 400 * 86400
    assert c.s.dot_chat.status(c.room, dot)['notification_renewal_managed_by'] == 'host'
    assert c.s.dots.find(dot['id'])['checkpoint'] == before
    assert c.s.channel_owner.current(binding['binding_id'])['state'] == 'active'


def test_unlimited_count_requires_distinct_explicit_consent_and_preserves_legacy_payloads():
    from hub.collaboration.channel_owner_authority import OwnerAuthority

    base = {'project': 'proj', 'binding_id': 'binding', 'expected_binding_version': 1,
            'action': 'approve', 'confirm_peer_id': 'peer', 'confirm_owner_tasks': True,
            'confirm_until_revoked': True, 'idempotency_key': 'legacy-owner-request'}
    legacy = OwnerAuthority.model_validate(base).model_dump()
    assert legacy['budget_mode'] == 'lifetime'
    assert 'confirm_unlimited_budget' not in legacy
    assert 'confirm_budget_change' not in legacy
    assert 'confirm_task_count_budget' not in legacy
    assert 'max_delegations' not in legacy
    explicit_false = OwnerAuthority.model_validate({**base, 'confirm_unlimited_budget': False}).model_dump()
    assert explicit_false == legacy
    unlimited = OwnerAuthority.model_validate({
        **base, 'budget_mode': 'unlimited', 'confirm_unlimited_budget': True}).model_dump()
    assert unlimited['budget_mode'] == 'unlimited' and unlimited['confirm_unlimited_budget']


def test_explicit_unlimited_count_keeps_grants_scope_task_budgets_and_audit_usage(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    policy = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    before_policy = dict(policy)
    before_grants = c.s.store.all('SELECT * FROM grants ORDER BY id')
    before_tokens = c.s.store.all('SELECT * FROM tokens ORDER BY id')
    current = w.binding(binding['binding_id'], c.owner, c.room)
    options = c.s.channel_owner.options(current)
    assert options['available_budget_modes'] == ['lifetime', 'rolling_24h', 'unlimited']
    assert options['max_delegations'] == 100

    approved, raw = approve(c, w, binding, scope, budget_mode='unlimited', confirm_unlimited_budget=True)
    auth = approved['owner_authorization']
    assert auth['budget_mode'] == 'unlimited' and auth['max_delegations'] is None
    assert auth['host_approval_required'] and not auth['changes_underlying_grant']
    assert c.s.channel_owner.control(raw, c.owner) == approved
    stored = c.s.channel_owner.current(binding['binding_id'])
    saved_scope = json.loads(stored['scope'])
    assert saved_scope['budget_mode'] == 'unlimited' and saved_scope['max_delegations'] is None
    policy = c.s.delegation.policy(c.room, policy['id'])
    for field in ('spec', 'digest', 'version', 'grant_id', 'grant_snapshot', 'principal', 'uses'):
        assert policy[field] == before_policy[field]
    assert c.s.store.all('SELECT * FROM grants ORDER BY id') == before_grants
    assert c.s.store.all('SELECT * FROM tokens ORDER BY id') == before_tokens

    c.s.store.execute('UPDATE delegation_policies SET uses=100 WHERE id=?', (policy['id'],))
    source = fresh(c, w, account)
    item = next(i for i in inbox(c)['items'] if i['message_id'] == source)
    created = call(c, item['task_request'])
    assert call(c, item['task_request'])['delegation_id'] == created['delegation_id']
    policy = c.s.delegation.policy(c.room, policy['id'])
    assert c.s.channel_owner.budget_limit(policy) is None
    assert policy['uses'] == c.s.channel_owner.budget_used(policy) == 101
    view = c.s.delegation.view(policy, c.owner, c.room)
    assert view['usage'] == {'delegations': 101, 'total_delegations': 101,
                            'window_delegations': 101, 'budget_mode': 'unlimited', 'max_delegations': None}
    assert view['budget'] == json.loads(before_policy['spec'])['budget']
    link = c.s.store.one('SELECT * FROM delegation_requests WHERE id=?', (created['delegation_id'],))
    goal = c.s.store.one('SELECT * FROM coordination_goals WHERE id=?', (link['goal_id'],))
    assert 0 < goal['expires_at'] - c.clock[0] <= 3600
    assert len(c.s.store.all('SELECT * FROM coordination_work')) == 1


@pytest.mark.parametrize('change', [
    {'expected_binding_version': 999},
    {'expected_authority_version': 999},
    {'expected_scope_digest': 'f' * 64},
    {'confirm_peer_id': 'another-peer'},
])
def test_unlimited_count_still_checks_exact_current_owner_scope(collab, change):
    c, w, account, binding, scope = fixture_channel(collab)
    policy = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    before = dict(policy)
    with pytest.raises(DevError):
        approve(c, w, binding, scope, budget_mode='unlimited', confirm_unlimited_budget=True, **change)
    assert c.s.delegation.policy(c.room, policy['id']) == before
    assert c.s.channel_owner.current(binding['binding_id']) is None
    no_work(c)


def test_existing_lifetime_approval_cannot_be_silently_replaced_with_unlimited(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    before = c.s.channel_owner.current(binding['binding_id'])
    with pytest.raises(DevError) as caught:
        approve(c, w, binding, scope, budget_mode='unlimited', confirm_unlimited_budget=True)
    assert caught.value.code == 'CHANNEL_OWNER_ALREADY_APPROVED'
    assert c.s.channel_owner.current(binding['binding_id']) == before
    policy = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    assert c.s.channel_owner.budget_mode(policy) == 'lifetime'
    assert c.s.channel_owner.budget_limit(policy) == 100


def test_revoking_unlimited_restores_original_count_limit_without_resetting_usage(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, budget_mode='unlimited', confirm_unlimited_budget=True)
    policy = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    c.s.store.execute('UPDATE delegation_policies SET uses=101 WHERE id=?', (policy['id'],))
    current = w.binding(binding['binding_id'], c.owner, c.room)
    auth = c.s.channel_owner.current(current['id'])
    c.s.channel_owner.control({**scope, 'binding_id': current['id'],
        'expected_binding_version': current['version'], 'expected_authority_version': auth['version'],
        'action': 'revoke', 'idempotency_key': key()}, c.owner)
    policy = c.s.delegation.policy(c.room, policy['id'])
    assert c.s.channel_owner.budget_mode(policy) == 'lifetime'
    assert c.s.channel_owner.budget_limit(policy) == 100
    assert c.s.channel_owner.budget_used(policy) == 101
    approved, _ = approve(c, w, binding, scope)
    assert approved['owner_authorization']['max_delegations'] == 100
    assert approved['owner_authorization']['budget_mode'] == 'lifetime'
    with pytest.raises(DevError) as caught:
        task(c, fresh(c, w, account))
    assert caught.value.code == 'DELEGATION_BUDGET_EXCEEDED'


@pytest.mark.parametrize('change', [
    {'max_delegations': 2},
    {'max_delegations': 0, 'confirm_task_count_budget': True},
    {'max_delegations': -1, 'confirm_task_count_budget': True},
    {'max_delegations': True, 'confirm_task_count_budget': True},
    {'max_delegations': 1.5, 'confirm_task_count_budget': True},
    {'max_delegations': '2', 'confirm_task_count_budget': True},
    {'budget_mode': 'unlimited', 'confirm_unlimited_budget': True,
     'max_delegations': 2, 'confirm_task_count_budget': True},
])
def test_explicit_task_count_is_positive_integer_and_not_an_ambiguous_unlimited_limit(collab, change):
    c, w, account, binding, scope = fixture_channel(collab)
    with pytest.raises(DevError):
        approve(c, w, binding, scope, **change)
    assert c.s.channel_owner.current(binding['binding_id']) is None
    no_work(c)


@pytest.mark.parametrize('mode', ['lifetime', 'rolling_24h'])
def test_custom_count_is_enforced_with_existing_usage_and_window_semantics(collab, mode):
    c, w, account, binding, scope = fixture_channel(collab)
    approved, _ = approve(c, w, binding, scope, budget_mode=mode, confirm_daily_budget=mode == 'rolling_24h',
                         max_delegations=2, confirm_task_count_budget=True)
    assert approved['owner_authorization']['max_delegations'] == 2
    for number in (2, 3):
        task(c, fresh(c, w, account, number))
    policy = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    assert c.s.channel_owner.budget_limit(policy) == 2
    assert c.s.delegation.view(policy, c.owner, c.room)['usage']['max_delegations'] == 2
    with pytest.raises(DevError) as caught:
        task(c, fresh(c, w, account, 4))
    assert caught.value.code == 'DELEGATION_BUDGET_EXCEEDED'
    c.clock[0] += 86401
    source = fresh(c, w, account, 5)
    if mode == 'rolling_24h':
        task(c, source)
        assert c.s.channel_owner.budget_used(policy) == 1
        assert c.s.delegation.policy(c.room, policy['id'])['uses'] == 3
    else:
        with pytest.raises(DevError) as caught:
            task(c, source)
        assert caught.value.code == 'DELEGATION_BUDGET_EXCEEDED'


def test_explicit_count_may_exceed_legacy_policy_cap_without_changing_policy_scope(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, max_delegations=200, confirm_task_count_budget=True)
    policy = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    assert json.loads(policy['spec'])['max_delegations'] == 100
    c.s.store.execute('UPDATE delegation_policies SET uses=199 WHERE id=?', (policy['id'],))
    task(c, fresh(c, w, account))
    with pytest.raises(DevError) as caught:
        task(c, fresh(c, w, account, 3))
    assert caught.value.code == 'DELEGATION_BUDGET_EXCEEDED'


@pytest.mark.parametrize('claimed', [False, True])
def test_budget_update_is_atomic_stops_unfinished_work_and_rejects_old_event_replay(collab, claimed):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    source = fresh(c, w, account)
    request = next(i for i in inbox(c)['items'] if i['message_id'] == source)['task_request']
    original = call(c, request)
    link = c.s.store.one('SELECT * FROM delegation_requests WHERE id=?', (original['delegation_id'],))
    if claimed:
        pending = call(c, inbox(c)['work_inbox_request'])
        item = next(item for item in pending['items'] if item['category'] == 'claimable')
        call(c, item['claim_request'])
    before = c.s.channel_owner.current(binding['binding_id'])
    old_request_count = len(c.s.store.all('SELECT * FROM delegation_requests'))
    updated, raw = approve(c, w, binding, scope, budget_mode='unlimited',
                          confirm_unlimited_budget=True, confirm_budget_change=True)
    assert updated['owner_authorization']['id'] == before['id']
    assert updated['owner_authorization']['version'] == before['version'] + 1
    assert updated['owner_authorization']['max_delegations'] is None
    assert c.s.channel_owner.control(raw, c.owner) == updated
    work = c.s.store.one('SELECT * FROM coordination_work WHERE id=?', (link['work_item_id'],))
    assert work['state'] == 'blocked' and work['lease_until'] is None
    for stale in (request, original['read_request']):
        with pytest.raises(DevError):
            call(c, stale)
    assert len(c.s.store.all('SELECT * FROM delegation_requests')) == old_request_count
    fresh_result = task(c, fresh(c, w, account, 3))
    assert fresh_result['delegation_id'] != original['delegation_id']


@pytest.mark.parametrize('change', [
    {'expected_binding_version': 999},
    {'expected_authority_version': 999},
    {'expected_scope_digest': 'f' * 64},
    {'confirm_peer_id': 'another-peer'},
])
def test_stale_budget_update_cannot_revoke_or_change_existing_authority(collab, change):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    result = task(c, fresh(c, w, account))
    before = c.s.channel_owner.current(binding['binding_id'])
    work_before = c.s.store.all('SELECT * FROM coordination_work')
    with pytest.raises(DevError):
        approve(c, w, binding, scope, budget_mode='unlimited',
                confirm_unlimited_budget=True, confirm_budget_change=True, **change)
    assert c.s.channel_owner.current(binding['binding_id']) == before
    assert c.s.store.all('SELECT * FROM coordination_work') == work_before
    call(c, result['read_request'])


def test_omitted_count_on_explicit_mode_update_preserves_previous_finite_limit(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, max_delegations=7, confirm_task_count_budget=True)
    approved, _ = approve(c, w, binding, scope, budget_mode='rolling_24h',
                         confirm_daily_budget=True, confirm_budget_change=True)
    assert approved['owner_authorization']['max_delegations'] == 7
    policy = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    assert c.s.channel_owner.budget_limit(policy) == 7


def test_budget_update_failure_rolls_back_revoke_and_source_fence(collab, monkeypatch):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    source = fresh(c, w, account)
    original = task(c, source)
    before = c.s.channel_owner.current(binding['binding_id'])
    work_before = c.s.store.all('SELECT * FROM coordination_work')
    policy_before = c.s.dots.policy(c.room, c.s.dots.find(c.slot['id']))
    original_valid = w.valid
    def reject_updated_binding(identifier):
        if c.s.channel_owner.current(identifier)['version'] != before['version']:
            raise DevError('SYNTHETIC_FINAL_VALIDATION_FAILED', 'Test validation failure', 409)
        return original_valid(identifier)
    monkeypatch.setattr(w, 'valid', reject_updated_binding)
    with pytest.raises(DevError) as caught:
        approve(c, w, binding, scope, budget_mode='unlimited',
                confirm_unlimited_budget=True, confirm_budget_change=True)
    assert caught.value.code == 'SYNTHETIC_FINAL_VALIDATION_FAILED'
    assert c.s.channel_owner.current(binding['binding_id']) == before
    assert c.s.store.all('SELECT * FROM coordination_work') == work_before
    assert c.s.delegation.policy(c.room, policy_before['id']) == policy_before
    call(c, original['read_request'])


@pytest.mark.parametrize('mode', ['lifetime', 'rolling_24h', 'unlimited'])
def test_persistent_slot_and_policy_ignore_old_expiry_until_their_own_authority_is_revoked(collab, mode):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope, budget_mode=mode, confirm_daily_budget=mode == 'rolling_24h',
            confirm_unlimited_budget=mode == 'unlimited')
    dot = c.s.dots.find(c.slot['id'])
    old_expiry = c.clock[0] - 1
    c.s.store.execute('UPDATE collaboration_join_slots SET expires_at=? WHERE id=?', (old_expiry, dot['id']))
    c.s.store.execute('UPDATE delegation_policies SET expires_at=? WHERE id=?', (old_expiry, dot['policy_id']))

    def slot_view():
        slot = c.s.joining.slot(c.room, dot['id'])
        return c.s.dots.slot_view(c.room, slot, dot, c.owner)

    def policy_view():
        policy = c.s.delegation.policy(c.room, dot['policy_id'])
        return c.s.delegation.view(policy, c.owner, c.room)

    joined = slot_view()
    assert joined['expires_at'] == old_expiry
    assert joined['state'] == 'registered'
    assert joined['effective_active'] is True
    assert joined['lifetime_mode'] == 'until_revoked'
    delegated = policy_view()
    assert delegated['effective_active'] is True
    assert delegated['lifetime_mode'] == 'until_revoked'
    assert delegated['expires_at'] is None

    current = w.binding(binding['binding_id'], c.owner, c.room)
    auth = c.s.channel_owner.current(current['id'])
    c.s.channel_owner.control({**scope, 'binding_id': current['id'],
        'expected_binding_version': current['version'], 'expected_authority_version': auth['version'],
        'action': 'revoke', 'idempotency_key': key()}, c.owner)
    delegated = policy_view()
    assert delegated['effective_active'] is False
    assert delegated['lifetime_mode'] == 'timed'
    assert delegated['expires_at'] == old_expiry
    # Chat consent is independently until-revoked; removing task authority
    # must not pretend the otherwise active channel has disconnected.
    assert slot_view()['effective_active'] is True

    current = w.binding(binding['binding_id'], c.owner, c.room)
    w.control({**scope, 'binding_id': current['id'], 'expected_version': current['version'],
               'action': 'revoke', 'idempotency_key': key()}, c.owner)
    joined = slot_view()
    assert joined['effective_active'] is False
    assert joined['state'] in {'expired', 'revoked'}


@pytest.mark.parametrize('mode', ['lifetime', 'rolling_24h', 'unlimited'])
@pytest.mark.parametrize('claimed', [False, True])
def test_identical_budget_update_is_noop_even_with_new_idempotency_keys(collab, mode, claimed):
    c, w, account, binding, scope = fixture_channel(collab)
    choice = {'budget_mode': mode, 'confirm_daily_budget': mode == 'rolling_24h',
              'confirm_unlimited_budget': mode == 'unlimited'}
    if mode != 'unlimited':
        choice.update(max_delegations=7, confirm_task_count_budget=True)
    approved, _ = approve(c, w, binding, scope, **choice)
    source = fresh(c, w, account)
    request = next(i for i in inbox(c)['items'] if i['message_id'] == source)['task_request']
    original = call(c, request)
    if claimed:
        pending = call(c, inbox(c)['work_inbox_request'])
        item = next(item for item in pending['items'] if item['category'] == 'claimable')
        call(c, item['claim_request'])
    before_auth = c.s.channel_owner.current(binding['binding_id'])
    before_binding = w.binding(binding['binding_id'], c.owner, c.room)
    before_work = c.s.store.all('SELECT * FROM coordination_work')
    before_evidence = c.s.store.all('SELECT * FROM channel_owner_messages')
    for _ in range(2):
        same, raw = approve(c, w, binding, scope, confirm_budget_change=True, **choice)
        assert same == approved
        assert c.s.channel_owner.control(raw, c.owner) == same
        assert c.s.channel_owner.current(binding['binding_id']) == before_auth
        assert w.binding(binding['binding_id'], c.owner, c.room) == before_binding
        assert c.s.store.all('SELECT * FROM coordination_work') == before_work
        assert c.s.store.all('SELECT * FROM channel_owner_messages') == before_evidence
        assert call(c, request)['delegation_id'] == original['delegation_id']
        call(c, original['read_request'])
        choice.pop('max_delegations', None)


@pytest.mark.parametrize('change', [
    {'expected_binding_version': 999},
    {'expected_authority_version': 999},
    {'expected_scope_digest': 'f' * 64},
    {'confirm_peer_id': 'another-peer'},
])
def test_same_budget_noop_still_rejects_stale_versions_scope_and_identity(collab, change):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    original = task(c, fresh(c, w, account))
    before = c.s.channel_owner.current(binding['binding_id'])
    work_before = c.s.store.all('SELECT * FROM coordination_work')
    with pytest.raises(DevError):
        approve(c, w, binding, scope, budget_mode='lifetime', confirm_budget_change=True, **change)
    assert c.s.channel_owner.current(binding['binding_id']) == before
    assert c.s.store.all('SELECT * FROM coordination_work') == work_before
    call(c, original['read_request'])


def test_same_budget_noop_cannot_accept_a_changed_live_peer(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    before = c.s.channel_owner.current(binding['binding_id'])
    c.s.store.execute("UPDATE weixin_bindings SET peer_id='changed-peer',version=version+1 WHERE id=?",
                      (binding['binding_id'],))
    # The helper sends the latest binding version and peer; matching the count
    # cannot reapprove a different identity using the old authority record.
    with pytest.raises(DevError) as caught:
        approve(c, w, binding, scope, budget_mode='lifetime', confirm_budget_change=True)
    assert caught.value.code == 'CHANNEL_OWNER_SCOPE_CHANGED'
    assert c.s.channel_owner.current(binding['binding_id']) == before


@pytest.mark.parametrize('validation', ['channel', 'policy'])
def test_same_budget_noop_does_not_skip_final_live_validation(collab, monkeypatch, validation):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
    before = c.s.channel_owner.current(binding['binding_id'])
    def deny(*args, **kwargs):
        raise DevError('SYNTHETIC_LIVE_VALIDATION_FAILED', 'Test validation failure', 409)
    if validation == 'channel':
        monkeypatch.setattr(w, 'valid', deny)
    else:
        monkeypatch.setattr(c.s.delegation, 'authorize_policy', deny)
    with pytest.raises(DevError) as caught:
        approve(c, w, binding, scope, budget_mode='lifetime', confirm_budget_change=True)
    assert caught.value.code == 'SYNTHETIC_LIVE_VALIDATION_FAILED'
    assert c.s.channel_owner.current(binding['binding_id']) == before
