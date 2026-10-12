"""Explicit owner task authority never comes from a peer name or a pasted claim."""
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
def test_revocation_or_scope_change_stops_existing_source_and_pending_task(collab, change):
    c, w, account, binding, scope = fixture_channel(collab)
    approve(c, w, binding, scope)
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
