"""Same-bot QR recovery with synthetic credentials and provider replies only."""
import asyncio
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import inbox, no_work
from tests.test_dot_weixin_channel import packet
from tests.test_weixin_qr_enrollment import (
    accept_args, confirm, enrollment as enrollment, start, status,
)


def account_for(c, scope, *, bot='synthetic-bot'):
    return c.s.weixin.import_account({
        **scope, 'idempotency_key': key(), 'bot_id': bot, 'label': 'Original synthetic account',
        'token': 'original-synthetic-secret', 'confirm_credential_storage': True,
        'confirm_provider_terms': True,
    }, c.owner)


def pair_account(c, scope, account):
    w = c.s.weixin
    binding = w.bind({
        **scope, 'account_id': account['account_id'], 'dot_id': c.slot['id'],
        'confirm_external_data_ingress': True, 'allow_replies': True, 'idempotency_key': key(),
    }, c.owner)
    w.commit_updates(w.claim_poll(account['account_id']), packet(1, binding['pairing_challenge']))
    row = w.binding(binding['binding_id'], c.owner, c.room)
    w.control({
        **scope, 'binding_id': row['id'], 'expected_version': row['version'],
        'action': 'activate', 'confirm_peer_id': 'synthetic-peer', 'idempotency_key': key(),
    }, c.owner)
    c.clock[0] += 3
    return w.binding(binding['binding_id'], c.owner, c.room)


def begin_renewal(c, q, scope, account):
    return start(q, scope, c.owner, renew_account_id=account['account_id'],
                 expected_credential_version=account['credential_version'])


def confirmed_renewal(c, q, scope, provider, account):
    _, view = begin_renewal(c, q, scope, account)
    confirm(provider)
    return status(q, scope, c.owner, view)


def original_credential(c, account):
    row = c.s.weixin.account(account['account_id'], c.owner)
    assert row['credential_version'] == account['credential_version']
    assert c.s.store.decrypt(row['secret']) == 'original-synthetic-secret'
    return row


def test_reauthentication_requires_fresh_scanner_and_separate_replace_consent(enrollment):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    old = pair_account(c, scope, account)
    before = original_credential(c, account)
    view = confirmed_renewal(c, q, scope, provider, account)
    assert view['renew_account_id'] == account['account_id']
    assert view['expected_credential_version'] == 1
    assert view['renew_bot_id'] == account['bot_id']
    assert not view['binding_preserved'] and view['re_pair_required']
    args = accept_args(scope, view)
    with pytest.raises(DevError) as missing:
        q.accept(args, c.owner)
    assert missing.value.code == 'WEIXIN_REPLACEMENT_CONFIRMATION_REQUIRED'
    with pytest.raises(DevError) as wrong:
        q.accept({**args, 'confirm_replace_existing': True, 'confirm_scanner_id': 'other-scanner'}, c.owner)
    assert wrong.value.code == 'WEIXIN_SCANNER_MISMATCH'
    assert original_credential(c, account) == before
    assert c.s.weixin.binding(old['id'], c.owner, c.room) == old
    no_work(c)


def test_same_bot_reauth_rotates_once_fences_old_poll_and_requires_peer_revalidation(enrollment):
    c, q, scope, provider = enrollment
    w = c.s.weixin
    account = account_for(c, scope)
    old_binding = pair_account(c, scope, account)
    old_claim = w.claim_poll(account['account_id'])
    original = w.account(account['account_id'], c.owner)
    view = confirmed_renewal(c, q, scope, provider, account)
    args = {**accept_args(scope, view), 'confirm_replace_existing': True}
    result = q.accept(args, c.owner)
    assert q.accept(args, c.owner) == result
    assert result['account_id'] == account['account_id']
    assert result['credential_version'] == 2
    assert result['room_bound'] is False and result['re_pair_required'] is True
    current = w.account(account['account_id'], c.owner)
    assert current['generation'] == original['generation'] + 1
    assert current['poll_fence'] > old_claim['poll_fence']
    assert c.s.store.decrypt(current['secret']) == 'synthetic-qr-bot-secret'
    assert current['last_sync_at'] is None
    with pytest.raises(DevError):
        w.commit_updates(old_claim, packet(2, 'obsolete poll'))
    assert w.account(account['account_id'], c.owner) == current
    recovered = status(q, scope, c.owner, view)
    assert recovered['state'] == 'accepted' and recovered['credential_version'] == 2
    binding = w.binding(old_binding['id'], c.owner, c.room)
    assert binding['state'] == 'pairing'
    assert binding['generation'] == current['generation']
    assert binding['credential_version'] == 2
    assert binding['peer_id'] == old_binding['peer_id']
    with pytest.raises(DevError):
        w.valid(binding['id'])
    assert inbox(c)['items'] == []
    no_work(c)
    for table in ('collaboration_audit', 'collaboration_idempotency'):
        assert 'synthetic-qr-bot-secret' not in json.dumps(c.s.store.all('SELECT * FROM ' + table))
    assert 'token' not in q.payload(c.s.store.one('SELECT * FROM weixin_enrollments'))


def test_unbound_same_bot_reauthentication_does_not_create_a_binding(enrollment):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    view = confirmed_renewal(c, q, scope, provider, account)
    result = q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    assert result['account_id'] == account['account_id'] and result['credential_version'] == 2
    assert result['room_bound'] is False
    assert not c.s.store.all('SELECT * FROM weixin_bindings')
    no_work(c)


def test_cancel_wins_over_inflight_reauthentication_and_new_scan_does_not_reuse_callback(enrollment):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    args, view = begin_renewal(c, q, scope, account)
    assert asyncio.run(q.start(args, c.owner)) == view
    assert begin_renewal(c, q, scope, account)[1] == view and provider.starts == 1
    status_args, actor, _, claim = q.prepare_status({
        **scope, 'enrollment_id': view['enrollment_id'], 'expected_version': view['version'],
    }, c.owner)
    cancelled = q.cancel({**status_args, 'idempotency_key': key()}, c.owner)
    _, fresh = begin_renewal(c, q, scope, account)
    confirm(provider)
    late = q.finish_status(status_args, actor, claim, provider.response)
    assert late == cancelled and late['state'] == 'cancelled'
    assert fresh['enrollment_id'] != view['enrollment_id'] and provider.starts == 2
    original_credential(c, account)
    with pytest.raises(DevError):
        q.accept({**accept_args(scope, {**view, 'scanner_id': 'synthetic-scanner'}),
                  'confirm_replace_existing': True}, c.owner)
    no_work(c)


@pytest.mark.parametrize('extra', [
    {'expected_credential_version': 1},
    {'renew_account_id': 'missing-account', 'expected_credential_version': 1},
    {'renew_account_id': 'replace-id', 'expected_credential_version': 0},
    {'renew_account_id': 'replace-id', 'expected_credential_version': 2},
])
def test_reauthentication_requires_an_exact_existing_account_version_before_provider_call(enrollment, extra):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    extra = {**extra}
    if extra.get('renew_account_id') == 'replace-id':
        extra['renew_account_id'] = account['account_id']
    with pytest.raises(DevError):
        start(q, scope, c.owner, **extra)
    assert provider.starts == 0
    original_credential(c, account)


@pytest.mark.parametrize('field,value', [('owner_user_id', 'another-owner'), ('space_id', 'another-space')])
def test_reauthentication_cannot_select_another_owner_or_space_account(enrollment, field, value):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    c.s.store.execute('UPDATE weixin_accounts SET ' + field + '=? WHERE id=?', (value, account['account_id']))
    with pytest.raises(DevError) as caught:
        begin_renewal(c, q, scope, account)
    assert caught.value.code == 'WEIXIN_ACCOUNT_NOT_FOUND' and provider.starts == 0


def test_reauthentication_cannot_move_an_existing_binding_to_another_topic(enrollment):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    original = pair_account(c, scope, account)
    other = c.s.conversations.create({'title': 'Other', 'projects': [c.scope],
        'idempotency_key': key()}, c.owner)
    with pytest.raises(DevError) as caught:
        begin_renewal(c, q, {**scope, 'conversation_id': other['conversation']['id']}, account)
    assert caught.value.code == 'WEIXIN_RENEWAL_SCOPE' and provider.starts == 0
    assert c.s.weixin.binding(original['id'], c.owner, c.room) == original


@pytest.mark.parametrize('provider_change', [
    {'ilink_bot_id': 'different-bot'},
    {'baseurl': 'https://unapproved.example'},
    {'bot_token': 'invalid\ncredential'},
])
def test_wrong_bot_or_provider_cannot_replace_credentials(enrollment, provider_change):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    _, view = begin_renewal(c, q, scope, account)
    confirm(provider)
    provider.response.update(provider_change)
    view = status(q, scope, c.owner, view)
    assert view['state'] == 'attention'
    assert 'scanner_id' not in view and 'qrcode_image' not in view
    original_credential(c, account)
    assert len(c.s.store.all('SELECT * FROM weixin_accounts')) == 1
    row = c.s.store.one('SELECT * FROM weixin_enrollments')
    assert 'token' not in q.payload(row)


@pytest.mark.parametrize('phase', ['status', 'callback', 'accept'])
def test_concurrent_credential_rotation_invalidates_older_qr(enrollment, phase):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    _, view = begin_renewal(c, q, scope, account)
    confirm(provider)
    raw = {**scope, 'enrollment_id': view['enrollment_id'], 'expected_version': view['version']}
    if phase == 'accept':
        view = status(q, scope, c.owner, view)
    elif phase == 'callback':
        args, actor, _, claim = q.prepare_status(raw, c.owner)
    c.s.weixin.import_account({
        **scope, 'idempotency_key': key(), 'bot_id': account['bot_id'], 'label': 'Concurrent rotation',
        'token': 'newer-synthetic-secret', 'expected_version': 1,
        'confirm_credential_storage': True, 'confirm_provider_terms': True,
    }, c.owner)
    with pytest.raises(DevError) as caught:
        if phase == 'accept':
            q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
        elif phase == 'callback':
            q.finish_status(args, actor, claim, provider.response)
        else:
            status(q, scope, c.owner, view)
    assert caught.value.code == 'WEIXIN_RENEWAL_CHANGED'
    current = c.s.weixin.account(account['account_id'], c.owner)
    assert current['credential_version'] == 2
    assert c.s.store.decrypt(current['secret']) == 'newer-synthetic-secret'


@pytest.mark.parametrize('action', ['pause', 'revoke'])
def test_binding_control_during_scan_wins_and_original_credentials_remain(enrollment, action):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    binding = pair_account(c, scope, account)
    view = confirmed_renewal(c, q, scope, provider, account)
    c.s.weixin.control({**scope, 'binding_id': binding['id'], 'expected_version': binding['version'],
                        'action': action, 'idempotency_key': key()}, c.owner)
    with pytest.raises(DevError) as caught:
        q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    assert caught.value.code == 'WEIXIN_RENEWAL_CHANGED'
    original_credential(c, account)
    assert c.s.weixin.binding(binding['id'], c.owner, c.room)['state'] == (
        'revoked' if action == 'revoke' else 'paused')
    assert q.cancel({**scope, 'enrollment_id': view['enrollment_id'], 'expected_version': view['version'],
                     'idempotency_key': key()}, c.owner)['state'] == 'cancelled'


@pytest.mark.parametrize('column,value', [('peer_id', 'different-peer'), ('allow_replies', 0)])
def test_unversioned_binding_security_change_is_not_implicitly_accepted(enrollment, column, value):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    binding = pair_account(c, scope, account)
    view = confirmed_renewal(c, q, scope, provider, account)
    c.s.store.execute('UPDATE weixin_bindings SET ' + column + '=? WHERE id=?', (value, binding['id']))
    with pytest.raises(DevError) as caught:
        q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    assert caught.value.code == 'WEIXIN_RENEWAL_CHANGED'
    original_credential(c, account)


def test_existing_scan_cannot_be_repurposed_as_reauthentication(enrollment):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    _, view = start(q, scope, c.owner)
    with pytest.raises(DevError) as caught:
        begin_renewal(c, q, scope, account)
    assert caught.value.code == 'WEIXIN_QR_SESSION_CONFLICT' and provider.starts == 1
    confirm(provider)
    view = status(q, scope, c.owner, view)
    with pytest.raises(DevError) as caught:
        q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    assert caught.value.code == 'WEIXIN_ACCOUNT_EXISTS'
    original_credential(c, account)


def test_original_principal_must_still_own_account_when_accepting(enrollment):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    view = confirmed_renewal(c, q, scope, provider, account)
    c.s.store.execute('UPDATE weixin_accounts SET owner_user_id=? WHERE id=?',
                      ('another-owner', account['account_id']))
    with pytest.raises(DevError) as caught:
        q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    assert caught.value.code == 'WEIXIN_ACCOUNT_NOT_FOUND'
    assert c.s.store.one('SELECT credential_version FROM weixin_accounts')['credential_version'] == 1


def test_channel_disable_wins_over_reauthentication_callback(enrollment):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    _, view = begin_renewal(c, q, scope, account)
    raw = {**scope, 'enrollment_id': view['enrollment_id'], 'expected_version': view['version']}
    args, actor, _, claim = q.prepare_status(raw, c.owner)
    c.s.channel_settings.control({**scope, 'channel': 'weixin', 'enabled': False,
        'expected_version': 0, 'idempotency_key': key()}, c.owner)
    confirm(provider)
    with pytest.raises(DevError):
        q.finish_status(args, actor, claim, provider.response)
    original_credential(c, account)
    row = c.s.store.one('SELECT * FROM weixin_enrollments')
    assert row['state'] == 'cancelled' and not row['payload']


def approve_owner_tasks(c, scope, binding):
    authority = c.s.channel_owner
    options = authority.options(binding)
    assert options['available']
    authority.control({
        **scope, 'binding_id': binding['id'], 'expected_binding_version': binding['version'],
        'expected_authority_version': 0, 'action': 'approve', 'confirm_peer_id': binding['peer_id'],
        'confirm_owner_tasks': True, 'confirm_until_revoked': True,
        'expected_scope_digest': options['scope_digest'], 'idempotency_key': key(),
    }, c.owner)
    return authority.current(binding['id'])


def fresh_challenge(c, scope, binding_id):
    return next(row for row in c.s.weixin.listing(scope, c.owner)['bindings']
                if row['id'] == binding_id)['pairing_challenge']


def activate_pending_peer(c, scope, binding_id, peer='synthetic-peer'):
    binding = c.s.weixin.binding(binding_id, c.owner, c.room)
    return c.s.weixin.control({
        **scope, 'binding_id': binding_id, 'expected_version': binding['version'],
        'action': 'activate', 'confirm_peer_id': peer, 'idempotency_key': key(),
    }, c.owner)


def test_owner_task_consent_waits_for_fresh_same_peer_proof_and_does_not_replay_old_tasks(enrollment):
    c, q, scope, provider = enrollment
    w = c.s.weixin
    account = account_for(c, scope)
    binding = pair_account(c, scope, account)
    original_auth = approve_owner_tasks(c, scope, binding)
    w.commit_updates(w.claim_poll(account['account_id']), packet(
        2, 'Old authorized message', create_time_ms=int(c.clock[0] * 1000)))
    old_message = inbox(c)['items'][0]
    assert old_message['task_request'] is not None
    original_scope = json.loads(original_auth['scope'])
    view = confirmed_renewal(c, q, scope, provider, account)
    q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    pending = c.s.channel_owner.current(binding['id'])
    assert pending['state'] == 'pending_revalidation'
    assert json.loads(pending['scope']) == original_scope
    assert inbox(c)['items'][0]['task_request'] is None
    challenge = fresh_challenge(c, scope, binding['id'])
    w.commit_updates(w.claim_poll(account['account_id']), packet(3, challenge))
    assert c.s.channel_owner.current(binding['id'])['state'] == 'pending_revalidation'
    activate_pending_peer(c, scope, binding['id'])
    current = c.s.channel_owner.current(binding['id'])
    assert current['state'] == 'active' and current['version'] > pending['version']
    new_scope = json.loads(current['scope'])
    for field in original_scope:
        if field not in {'credential_version', 'generation'}:
            assert new_scope[field] == original_scope[field]
    assert new_scope['credential_version'] == 2
    assert new_scope['generation'] == original_scope['generation'] + 1
    assert inbox(c)['items'][0]['task_request'] is None
    c.clock[0] += 3
    w.commit_updates(w.claim_poll(account['account_id']), packet(
        4, 'Fresh authorized message', create_time_ms=int(c.clock[0] * 1000)))
    newest = next(item for item in inbox(c)['items'] if item['message_id'] != old_message['message_id'])
    assert newest['task_request'] is not None
    no_work(c)


@pytest.mark.parametrize('with_authority', [False, True])
def test_reauthentication_different_peer_cannot_inherit_original_channel(enrollment, with_authority):
    c, q, scope, provider = enrollment
    w = c.s.weixin
    account = account_for(c, scope)
    binding = pair_account(c, scope, account)
    if with_authority:
        approve_owner_tasks(c, scope, binding)
    view = confirmed_renewal(c, q, scope, provider, account)
    q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    challenge = fresh_challenge(c, scope, binding['id'])
    try:
        w.commit_updates(w.claim_poll(account['account_id']), packet(
            2, challenge, from_user_id='different-peer'))
    except DevError:
        pass
    with pytest.raises(DevError):
        activate_pending_peer(c, scope, binding['id'], peer='different-peer')
    current = w.binding(binding['id'], c.owner, c.room)
    assert current['state'] != 'active' and current['peer_id'] == 'synthetic-peer'
    if with_authority:
        assert c.s.channel_owner.current(binding['id'])['state'] == 'pending_revalidation'
    no_work(c)


@pytest.mark.parametrize('with_authority', [False, True])
def test_pause_resume_cannot_bypass_fresh_peer_proof_after_reauthentication(enrollment, with_authority):
    c, q, scope, provider = enrollment
    w = c.s.weixin
    account = account_for(c, scope)
    binding = pair_account(c, scope, account)
    if with_authority:
        approve_owner_tasks(c, scope, binding)
    view = confirmed_renewal(c, q, scope, provider, account)
    q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    current = w.binding(binding['id'], c.owner, c.room)
    try:
        w.control({**scope, 'binding_id': current['id'], 'expected_version': current['version'],
                   'action': 'pause', 'idempotency_key': key()}, c.owner)
    except DevError:
        pass
    current = w.binding(binding['id'], c.owner, c.room)
    with pytest.raises(DevError):
        w.control({**scope, 'binding_id': current['id'], 'expected_version': current['version'],
                   'action': 'resume', 'idempotency_key': key()}, c.owner)
    assert w.binding(binding['id'], c.owner, c.room)['state'] != 'active'
    if with_authority:
        assert c.s.channel_owner.current(binding['id'])['state'] == 'pending_revalidation'
    no_work(c)


@pytest.mark.parametrize('revoke_phase', ['during_scan', 'after_reauthentication'])
def test_explicit_task_consent_revocation_wins_over_credential_recovery(enrollment, revoke_phase):
    c, q, scope, provider = enrollment
    w = c.s.weixin
    account = account_for(c, scope)
    binding = pair_account(c, scope, account)
    approve_owner_tasks(c, scope, binding)
    view = confirmed_renewal(c, q, scope, provider, account)
    def revoke():
        current = w.binding(binding['id'], c.owner, c.room)
        auth = c.s.channel_owner.current(binding['id'])
        c.s.channel_owner.control({
            **scope, 'binding_id': binding['id'], 'expected_binding_version': current['version'],
            'expected_authority_version': auth['version'], 'action': 'revoke', 'idempotency_key': key(),
        }, c.owner)
    if revoke_phase == 'during_scan':
        revoke()
    q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    if revoke_phase == 'after_reauthentication':
        revoke()
    assert c.s.channel_owner.current(binding['id'])['state'] == 'revoked'
    challenge = fresh_challenge(c, scope, binding['id'])
    w.commit_updates(w.claim_poll(account['account_id']), packet(2, challenge))
    activate_pending_peer(c, scope, binding['id'])
    assert c.s.channel_owner.current(binding['id'])['state'] == 'revoked'
    c.clock[0] += 3
    w.commit_updates(w.claim_poll(account['account_id']), packet(
        3, 'No longer owner-authorized', create_time_ms=int(c.clock[0] * 1000)))
    assert inbox(c)['items'][0]['task_request'] is None
    no_work(c)


def test_unicode_scanner_confirmation_fails_as_a_safe_mismatch(enrollment):
    c, q, scope, provider = enrollment
    account = account_for(c, scope)
    view = confirmed_renewal(c, q, scope, provider, account)
    with pytest.raises(DevError) as caught:
        q.accept({**accept_args(scope, view), 'confirm_replace_existing': True,
                  'confirm_scanner_id': '错误身份'}, c.owner)
    assert caught.value.code == 'WEIXIN_SCANNER_MISMATCH'
    original_credential(c, account)


def test_expired_pairing_can_refresh_without_reenrolling_or_bypassing_peer_proof(enrollment):
    c, q, scope, provider = enrollment
    w = c.s.weixin
    account = account_for(c, scope)
    binding = pair_account(c, scope, account)
    approve_owner_tasks(c, scope, binding)
    view = confirmed_renewal(c, q, scope, provider, account)
    q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    old_challenge = fresh_challenge(c, scope, binding['id'])
    current = w.binding(binding['id'], c.owner, c.room)
    c.clock[0] = current['pairing_until'] + 1
    assert w.claim_poll(account['account_id']) is None
    refreshed = w.control({
        **scope, 'binding_id': binding['id'], 'expected_version': current['version'],
        'action': 'refresh_pairing', 'idempotency_key': key(),
    }, c.owner)
    challenge = refreshed['pairing_challenge']
    assert challenge != old_challenge
    assert c.s.channel_owner.current(binding['id'])['state'] == 'pending_revalidation'
    assert w.account(account['account_id'], c.owner)['credential_version'] == 2
    w.commit_updates(w.claim_poll(account['account_id']), packet(2, old_challenge))
    assert w.binding(binding['id'], c.owner, c.room)['state'] == 'pairing'
    c.clock[0] += 3
    w.commit_updates(w.claim_poll(account['account_id']), packet(3, challenge))
    assert c.s.channel_owner.current(binding['id'])['state'] == 'pending_revalidation'
    activate_pending_peer(c, scope, binding['id'])
    assert c.s.channel_owner.current(binding['id'])['state'] == 'active'
    assert provider.starts == 1
    no_work(c)


def test_attention_account_can_reauthenticate_without_silent_task_reactivation(enrollment):
    c, q, scope, provider = enrollment
    w = c.s.weixin
    account = account_for(c, scope)
    binding = pair_account(c, scope, account)
    approve_owner_tasks(c, scope, binding)
    w.poll_failed(w.claim_poll(account['account_id']),
                  DevError('WEIXIN_SESSION_EXPIRED', 'synthetic expired session', 403))
    assert w.account(account['account_id'], c.owner)['state'] == 'attention'
    view = confirmed_renewal(c, q, scope, provider, account)
    q.accept({**accept_args(scope, view), 'confirm_replace_existing': True}, c.owner)
    current = w.account(account['account_id'], c.owner)
    assert current['state'] == 'active' and current['failures'] == 0 and current['next_poll'] == 0
    assert not current['reason_code'] and current['last_sync_at'] is None
    assert w.binding(binding['id'], c.owner, c.room)['state'] == 'pairing'
    assert c.s.channel_owner.current(binding['id'])['state'] == 'pending_revalidation'
    no_work(c)
