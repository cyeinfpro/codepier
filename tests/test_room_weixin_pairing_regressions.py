"""Regressions for native-room WeChat pairing dead ends seen with real accounts.

Every case uses the synthetic provider and the real ledgers. They cover inputs
that the earlier synthetic happy path never produced: typed pairing codes,
provider timestamps, an already registered WeChat and leftover bindings.
"""
import uuid

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import packet
from tests.test_room_managed_work import connected
from tests.test_room_weixin_native import setup_channel
from tests.test_weixin_qr_enrollment import confirm


async def start_flow(s, owner, provider, conversation_id, member_id=''):
    async def action(flow, verb, **extra):
        return (await s.room_channels.action({'flow_id': flow['id'], 'action': verb,
            'expected_version': flow['version'], 'idempotency_key': key(), **extra}, owner))['flow']
    flow = s.room_channels.create({'conversation_id': conversation_id, 'member_id': member_id,
        'idempotency_key': key()}, owner)['flow']
    return flow, action


async def bound_until_pairing(collab):
    ctx = await connected(collab)
    s, owner, worker, clock, identity, credentials = ctx
    from dataclasses import replace
    from types import SimpleNamespace
    s.config = replace(s.config, weixin_enabled=True)
    provider = SimpleNamespace(starts=0, response={'status': 'wait'})

    class Provider:
        async def qr_start(self):
            provider.starts += 1
            return {'qrcode': 'private-native-qr', 'qrcode_img_content': 'https://weixin.qq.com/synthetic'}

        async def qr_status(self, *args):
            return provider.response
    s.weixin.client_factory = lambda *_: Provider()
    flow, action = await start_flow(s, owner, provider, identity['conversation_id'], identity['member_id'])
    flow = await action(flow, 'start_qr', confirm_credential_storage=True, confirm_provider_terms=True)
    confirm(provider)
    flow = await action(flow, 'poll_qr')
    flow = await action(flow, 'accept_account', confirm_scanner_id=flow['qr']['scanner_id'])
    flow = await action(flow, 'bind', confirm_external_data_ingress=True, allow_replies=True,
        confirm_until_revoked=True)
    assert flow['stage'] == 'pairing'
    return ctx, flow, provider, action


@pytest.mark.asyncio
@pytest.mark.parametrize('typed', [
    lambda code: code.lower(),                                   # phone keyboards
    lambda code: '  ' + code[:12] + ' ' + code[12:] + '\n',     # stray spaces / newline
    lambda code: code.replace('-', '－'),                        # full-width dash (IME)
    lambda code: code[4:],                                       # prefix omitted
])
async def test_typed_pairing_code_variants_still_pair(collab, typed):
    (s, owner, worker, clock, identity, credentials), flow, provider, action = await bound_until_pairing(collab)
    code = flow['binding']['pairing_challenge']
    s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(1, typed(code), create_time_ms=int(clock[0] * 1000) + 5))
    flow = s.room_channels.read(flow['id'], owner)['flow']
    assert flow['stage'] == 'confirm_owner'
    assert flow['binding']['pending_peer_id'] == 'synthetic-peer'


@pytest.mark.asyncio
async def test_provider_update_time_and_clock_skew_do_not_block_pairing(collab):
    (s, owner, worker, clock, identity, credentials), flow, provider, action = await bound_until_pairing(collab)
    # Real iLink packets may carry update_time_ms on a first delivery, and the
    # provider clock can trail the Hub clock by a few seconds.
    created = int(clock[0] * 1000) - 4000
    s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(1, flow['binding']['pairing_challenge'], create_time_ms=created, update_time_ms=created))
    flow = s.room_channels.read(flow['id'], owner)['flow']
    assert flow['stage'] == 'confirm_owner'


@pytest.mark.asyncio
async def test_wrong_code_is_reported_to_the_panel_and_never_becomes_a_message(collab):
    (s, owner, worker, clock, identity, credentials), flow, provider, action = await bound_until_pairing(collab)
    s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(1, '你好', create_time_ms=int(clock[0] * 1000) + 5))
    flow = s.room_channels.read(flow['id'], owner)['flow']
    assert flow['stage'] == 'pairing'
    assert flow['pairing_feedback']['state'] == 'mismatch'
    assert not s.store.all("SELECT * FROM collaboration_messages WHERE origin='external_channel'")
    # The panel can show the inbound lane is alive and how to transfer the code.
    assert flow['inbound']['last_sync_at']
    assert flow['binding']['pairing_qr'].startswith('data:image/svg+xml;base64,')


@pytest.mark.asyncio
async def test_rescanning_registered_wechat_in_another_room_can_finish(collab):
    (s, owner, worker, clock, identity, credentials), first, provider, _ = await setup_channel(collab)
    # A second room scans the same WeChat instead of choosing the saved account.
    s2, owner2, worker2, clock2, identity2, credentials2 = await connected(collab)
    flow, action = await start_flow(s, owner, provider, identity2['conversation_id'], identity2['member_id'])
    flow = await action(flow, 'start_qr', confirm_credential_storage=True, confirm_provider_terms=True)
    confirm(provider)
    flow = await action(flow, 'poll_qr')
    assert flow['stage'] == 'confirm_account'
    assert flow['qr']['re_pair_required'] is True
    names = [field['name'] for field in flow['next_request']['form_fields']]
    assert 'confirm_replace_existing' in names
    with pytest.raises(DevError) as missing:
        await action(flow, 'accept_account', confirm_scanner_id=flow['qr']['scanner_id'])
    assert missing.value.code == 'WEIXIN_ACCOUNT_REUSE_CONFIRM'
    flow = await action(flow, 'accept_account', confirm_scanner_id=flow['qr']['scanner_id'],
        confirm_replace_existing=True)
    assert flow['account_id'] == first['account_id']
    assert flow['stage'] == 'route_conflict'
    flow = await action(flow, 'migrate', confirm_migrate_binding_id=flow['existing_binding']['id'],
        expected_binding_version=flow['existing_binding']['version'])
    assert flow['stage'] == 'confirm_binding'
    flow = await action(flow, 'bind', confirm_external_data_ingress=True, allow_replies=True,
        confirm_until_revoked=True)
    assert flow['stage'] == 'pairing'
    clock[0] += 3
    s.weixin.commit_updates(s.weixin.claim_poll(flow['account_id']),
        packet(91, flow['binding']['pairing_challenge'], create_time_ms=int(clock[0] * 1000)))
    flow = s.room_channels.read(flow['id'], owner)['flow']
    assert flow['stage'] == 'confirm_owner'


@pytest.mark.asyncio
async def test_leftover_binding_in_same_room_is_offered_for_migration_before_bind(collab):
    (s, owner, worker, clock, identity, credentials), first, provider, action = await setup_channel(collab)
    first = await action(first, 'pause')
    # Simulate the paused leftover on a different saved account (as left by a
    # room migration) and a fresh connect flow for the same room.
    account = s.store.one('SELECT * FROM weixin_accounts WHERE id=?', (first['account_id'],))
    other = uuid.uuid4().hex
    columns = list(account.keys())
    values = [other if c == 'id' else 'other-bot' if c == 'bot_id' else account[c] for c in columns]
    s.store.execute('INSERT INTO weixin_accounts (' + ','.join(columns) + ') VALUES (' + ','.join('?' * len(columns)) + ')', values)
    s.store.execute('UPDATE weixin_bindings SET account_id=? WHERE id=?', (other, first['binding']['id']))
    s.store.execute("UPDATE room_channel_flows SET state='closed' WHERE id=?", (first['id'],))
    flow, action = await start_flow(s, owner, provider, identity['conversation_id'], identity['member_id'])
    flow = await action(flow, 'select_account', account_id=first['account_id'])
    assert flow['stage'] == 'route_conflict'
    assert flow['existing_binding']['id'] == first['binding']['id']
    flow = await action(flow, 'migrate', confirm_migrate_binding_id=flow['existing_binding']['id'],
        expected_binding_version=flow['existing_binding']['version'])
    assert flow['stage'] == 'confirm_binding'
    flow = await action(flow, 'bind', confirm_external_data_ingress=True, allow_replies=True,
        confirm_until_revoked=True)
    assert flow['stage'] == 'pairing'


@pytest.mark.asyncio
async def test_room_without_lead_offers_member_choice_instead_of_add_dot(collab):
    s, owner, worker, clock, identity, credentials = await connected(collab)
    await connected(collab, conversation_id=identity['conversation_id'])
    s.store.execute("UPDATE conversation_rooms SET lead_member_id='' WHERE id=?", (identity['conversation_id'],))
    from dataclasses import replace
    s.config = replace(s.config, weixin_enabled=True)
    flow, action = await start_flow(s, owner, None, identity['conversation_id'])
    # Pretend the account step is already complete.
    account = uuid.uuid4().hex
    now = clock[0]
    s.store.execute('''INSERT INTO weixin_accounts (id,space_id,owner_user_id,bot_id,label,api_base,secret,state,
        credential_version,cursor,created,updated) VALUES (?,?,?,?,?,?,?,'active',1,?,?,?)''',
        (account, owner.space_id, owner.user_id, 'bot-' + account[:6], '微信', 'https://ilinkai.weixin.qq.com',
         s.store.encrypt('token'), s.store.encrypt(''), now, now))
    flow = await action(flow, 'select_account', account_id=account)
    assert flow['stage'] == 'choose_member'
    field = flow['next_request']['form_fields'][0]
    assert field['name'] == 'member_id' and len(field['options']) == 2
    flow = await action(flow, 'refresh', member_id=identity['member_id'])
    assert flow['stage'] == 'confirm_binding' and flow['member_id'] == identity['member_id']


@pytest.mark.asyncio
async def test_member_removed_during_setup_binds_the_member_shown_in_preview(collab):
    (s, owner, worker, clock, identity, credentials), flow, provider, action = await bound_until_pairing(collab)
    account_id = flow['account_id']
    flow = await action(flow, 'cancel')
    flow, action = await start_flow(s, owner, provider, identity['conversation_id'], identity['member_id'])
    replacement = (await connected(collab, conversation_id=identity['conversation_id']))[4]
    s.store.execute("UPDATE room_members SET status='paused' WHERE id=?", (identity['member_id'],))
    s.store.execute('UPDATE conversation_rooms SET lead_member_id=? WHERE id=?',
                    (replacement['member_id'], identity['conversation_id']))
    flow = await action(flow, 'select_account', account_id=account_id)
    assert flow['stage'] == 'confirm_binding'
    assert flow['member']['id'] == replacement['member_id']
    flow = await action(flow, 'bind', confirm_external_data_ingress=True, allow_replies=True,
                        confirm_until_revoked=True)
    assert flow['stage'] == 'pairing'
    binding = s.store.one('SELECT * FROM weixin_bindings WHERE id=?', (flow['binding']['id'],))
    assert binding['native_member_id'] == replacement['member_id']
