"""Explicit cutover fences old channel writes without deleting their ledgers.

All provider inputs and native callback hosts are synthetic. Fixtures perform
real joins, pairing, owner approval, safe pause and the public room cutover.
"""
from dataclasses import replace

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_channel_enrollment_flow import prepare, incoming, send as external_send, code
from tests.test_dot_weixin_channel import packet
from tests.test_dot_telegram_channel import post
from tests.test_collaboration_dot_chat import inbox, owner_message
from tests.test_collaboration_dots import call, lease
from tests.test_dot_weixin_owner_authority import task
from tests.test_room_managed_work import connected, save_room_permissions
from tests.test_room_migration_boundary import legacy
from tests.legacy_iam_fixture import seed_grant


def legacy_channel(collab, channel):
    c, transport, scope, account, raw = prepare(collab, channel)
    binding = transport.bind(raw, c.owner)
    external_send(c, transport, account, incoming(c, channel, 1, code(channel, binding)))
    source = external_send(c, transport, account, incoming(c, channel, 2, '保留原消息和渠道回执。'))['message_ids'][0]
    posted = post(c, source, '原渠道回复，保留原始标识。')
    current = transport.binding(binding['binding_id'], c.owner, c.room)
    assert current['state'] == 'active'
    return c, transport, scope, account, raw, current, source, posted


def remove_original_members(c, conversation_id):
    members = c.s.store.all("SELECT id FROM room_members WHERE conversation_id=? AND status!='removed'", (conversation_id,))
    assert members
    for member in members:
        flow = c.s.store.one('SELECT id FROM room_connect_flows WHERE member_id=?', (member['id'],))
        current = c.s.rooms.connect_read({'flow_id': flow['id']}, c.owner)['flow']
        c.s.rooms.connect_action({'flow_id': flow['id'], 'action': 'remove',
            'expected_version': current['version'], 'idempotency_key': key()}, c.owner)


def activate(c, conversation_id):
    room = c.s.rooms.read({'conversation_id': conversation_id}, c.owner)['room']
    raw = {'conversation_id': conversation_id, 'action': 'activate',
        'expected_version': room['version'], 'idempotency_key': key()}
    result = c.s.rooms.migrate_room(raw, c.owner)
    assert result['activated'] and not result['room']['legacy']
    assert c.s.rooms.migrate_room(raw, c.owner)['activated']
    return result


def outbox_evidence(store):
    """Safe stopping may change delivery status, but never its original data."""
    fields = {
        'weixin_deliveries': ('id', 'binding_id', 'generation', 'message_id', 'message_version',
            'message_digest', 'source_message_id', 'peer_id', 'created'),
        'weixin_parts': ('id', 'delivery_id', 'ordinal', 'client_id', 'kind', 'body', 'body_digest',
            'wire_body', 'provider_receipt', 'created'),
        'channel_mirrors': ('id', 'channel', 'binding_id', 'binding_version', 'generation',
            'credential_version', 'message_id', 'message_version', 'message_digest', 'room_id',
            'conversation_id', 'created'),
        'channel_mirror_parts': ('id', 'delivery_id', 'ordinal', 'kind', 'body', 'external_id', 'created'),
    }
    return {table: store.all('SELECT ' + ','.join(columns) + ' FROM ' + table + ' ORDER BY id')
        for table, columns in fields.items()}


def detached_legacy_partition(c, conversation_id):
    # Reducing existing scope is a safe stop, even before room activation.
    # Exercise the real owner route; keep the original partition and source.
    room = c.s.rooms.read({'conversation_id': conversation_id}, c.owner)['room']
    c.s.rooms.projects({'conversation_id': conversation_id, 'projects': [],
        'expected_version': room['version'], 'idempotency_key': key()}, c.owner)
    assert not c.s.store.all('SELECT * FROM conversation_projects WHERE conversation_id=?', (conversation_id,))


@pytest.mark.parametrize('channel', ['weixin', 'telegram'])
def test_legacy_channels_require_safe_pause_before_room_activation(collab, channel):
    c, transport, scope, account, bind_raw, binding, source, posted = legacy_channel(collab, channel)
    cid = scope['conversation_id']
    account_before = c.s.store.one('SELECT * FROM ' + channel + '_accounts WHERE id=?', (account['account_id'],))
    messages_before = c.s.store.all('SELECT * FROM collaboration_messages ORDER BY id')
    outbox_before = outbox_evidence(c.s.store)
    assert any(outbox_before.values())
    with pytest.raises(DevError) as required:
        c.s.room_channels.create({'conversation_id': cid, 'idempotency_key': key()}, c.owner)
    assert required.value.code == 'ROOM_MIGRATION_REQUIRED'
    assert not c.s.store.all('SELECT * FROM room_channel_flows')
    remove_original_members(c, cid)
    room = c.s.rooms.read({'conversation_id': cid}, c.owner)['room']
    with pytest.raises(DevError) as active_channel:
        c.s.rooms.migrate_room({'conversation_id': cid, 'action': 'activate',
            'expected_version': room['version'], 'idempotency_key': key()}, c.owner)
    assert active_channel.value.code == 'ROOM_MIGRATION_BLOCKED'
    pause = {'channel': channel, 'expected_version': binding['version'], 'idempotency_key': key()}
    stopped = c.s.room_channels.pause_legacy(cid, binding['id'], pause, c.owner)
    assert stopped['paused'] and stopped['binding']['state'] == 'paused'
    changes = c.s.store.one('SELECT total_changes() n')['n']
    assert c.s.room_channels.pause_legacy(cid, binding['id'], pause, c.owner) == stopped
    assert c.s.store.one('SELECT total_changes() n')['n'] == changes
    activate(c, cid)
    current = transport.binding(binding['id'], c.owner, c.room)
    assert current['state'] == 'paused'
    assert c.s.store.all('SELECT * FROM collaboration_messages ORDER BY id') == messages_before
    assert outbox_evidence(c.s.store) == outbox_before
    account_after = c.s.store.one('SELECT * FROM ' + channel + '_accounts WHERE id=?', (account['account_id'],))
    for field in ('secret', 'credential_version', 'generation'):
        assert account_after[field] == account_before[field]
    history = c.s.room_runtime.read(cid, c.owner)
    assert {source, posted['message']['id']} <= {m['id'] for m in history['messages']}
    assert transport.listing(scope, c.owner)
    before = c.s.store.one('SELECT total_changes() n')['n']
    with pytest.raises(DevError) as resume:
        transport.control({**scope, 'binding_id': binding['id'], 'expected_version': current['version'],
            'action': 'resume', 'idempotency_key': key()}, c.owner)
    assert resume.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    with pytest.raises(DevError) as bind:
        transport.bind({**bind_raw, 'idempotency_key': key()}, c.owner)
    assert bind.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    auth = c.s.channel_owner.current(binding['id'])
    with pytest.raises(DevError) as approval:
        c.s.channel_owner.control({**scope, 'binding_id': binding['id'],
            'expected_binding_version': current['version'], 'expected_authority_version': auth['version'],
            'action': 'approve', 'confirm_peer_id': binding.get('peer_id') or binding['chat_id'],
            'confirm_owner_tasks': True, 'confirm_until_revoked': True,
            'expected_scope_digest': 'a' * 64, 'idempotency_key': key()}, c.owner, channel=channel)
    assert approval.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    with pytest.raises(DevError) as files:
        if channel == 'weixin':
            transport.media.approve({**scope, 'source_message_id': source, 'expected_source_version': 1,
                'artifact_ids': ['original-artifact'], 'confirm_file_egress': True, 'idempotency_key': key()}, c.owner)
        else:
            transport.files.approve({**scope, 'binding_id': binding['id'], 'expected_binding_version': current['version'],
                'message_id': posted['message']['id'], 'expected_message_version': 1, 'preview_digest': 'a' * 64,
                'artifact_ids': ['original-artifact'], 'confirm_file_egress': True, 'idempotency_key': key()}, c.owner)
    assert files.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    assert c.s.store.one('SELECT total_changes() n')['n'] == before
    # A genuine owner can still revoke the original permission after cutover.
    c.s.channel_owner.control({**scope, 'binding_id': binding['id'],
        'expected_binding_version': current['version'], 'expected_authority_version': auth['version'],
        'action': 'revoke', 'idempotency_key': key()}, c.owner, channel=channel)
    assert c.s.channel_owner.current(binding['id'])['state'] == 'revoked'


@pytest.mark.parametrize('channel', ['weixin', 'telegram'])
@pytest.mark.parametrize('detach', [False, True])
def test_safe_pause_tracks_original_operation_with_or_without_last_project(collab, channel, detach):
    c, transport, scope, account, _, binding, _, _ = legacy_channel(collab, channel)
    cid = scope['conversation_id']
    source = external_send(c, transport, account, incoming(c, channel, 3))['message_ids'][0]
    started = task(c, source)
    pending = call(c, inbox(c)['work_inbox_request'])
    entry = next(item for item in pending['items'] if item['category'] == 'claimable')
    work = call(c, entry['claim_request'])['work_item']
    admitted = c.s.coordination.admit({**lease(c, work), 'tool': 'read',
        'arguments': {'path': 'README.md'}}, c.actor)
    operation_id = admitted['operation_id']
    original = c.s.store.one('SELECT * FROM operations WHERE id=?', (operation_id,))
    assert original['state'] == 'queued' and original['attempts'] == 0
    unrelated = None
    if not detach:
        panel, _ = owner_message(c, '另一个面板任务，不能随微信许可一起停止。')
        request = next(item['task_request'] for item in inbox(c)['items'] if item['message_id'] == panel['message']['id'])
        separate = call(c, request)
        separate_link = c.s.store.one('SELECT * FROM delegation_requests WHERE id=?', (separate['delegation_id'],))
        unrelated = c.s.store.one('SELECT * FROM coordination_goals WHERE id=?', (separate_link['goal_id'],))
    goals = c.s.store.all('SELECT id FROM coordination_goals ORDER BY id')
    data = outbox_evidence(c.s.store)
    account_before = c.s.store.one('SELECT * FROM ' + channel + '_accounts WHERE id=?', (account['account_id'],))
    if detach:
        detached_legacy_partition(c, cid)
    current = c.s.store.one('SELECT * FROM ' + channel + '_bindings WHERE id=?', (binding['id'],))
    if detach:
        with pytest.raises(DevError) as old_scope:
            transport.control({**scope, 'binding_id': binding['id'], 'expected_version': current['version'],
                'action': 'pause', 'idempotency_key': key()}, c.owner)
        assert old_scope.value.code == 'CONVERSATION_PROJECT_MISMATCH'
    operation_before_pause = c.s.store.one('SELECT * FROM operations WHERE id=?', (operation_id,))
    receipt_before_pause = c.s.store.one('SELECT * FROM coordination_operations WHERE operation_id=?', (operation_id,))
    raw = {'channel': channel, 'expected_version': current['version'], 'idempotency_key': key()}
    with pytest.raises(DevError) as stale:
        c.s.room_channels.pause_legacy(cid, binding['id'], {**raw, 'expected_version': current['version'] + 1}, c.owner)
    assert stale.value.code == 'STALE_VERSION'
    assert c.s.store.one('SELECT * FROM operations WHERE id=?', (operation_id,)) == operation_before_pause
    stopped = c.s.room_channels.pause_legacy(cid, binding['id'], raw, c.owner)
    assert stopped['paused']
    assert c.s.channel_owner.current(binding['id'])['state'] == 'revoked'
    after = c.s.store.one('SELECT * FROM operations WHERE id=?', (operation_id,))
    assert after['cancel_requested'] and after['attempts'] == original['attempts']
    assert after['state'] == 'cancelled'
    assert c.s.runtime.operation_result(after)['error']['code'] == 'CANCELLED'
    receipt = c.s.store.one('SELECT * FROM coordination_operations WHERE operation_id=?', (operation_id,))
    if detach:
        # Removing the project already cancelled this operation through the
        # original Runtime path. Channel pause must preserve that exact receipt,
        # not manufacture a second cancellation or relabel its original path.
        assert after == operation_before_pause
        assert receipt == receipt_before_pause
    else:
        assert receipt['cancel_note'] == 'cancelled_before_delivery'
    link = c.s.store.one('SELECT * FROM delegation_requests WHERE id=?', (started['delegation_id'],))
    assert receipt['goal_id'] == link['goal_id'] and receipt['work_item_id'] == work['id']
    assert c.s.store.one('SELECT state FROM coordination_goals WHERE id=?', (link['goal_id'],))['state'] == 'paused'
    assert outbox_evidence(c.s.store) == data
    assert c.s.store.all('SELECT id FROM coordination_goals ORDER BY id') == goals
    if unrelated:
        assert c.s.store.one('SELECT * FROM coordination_goals WHERE id=?', (unrelated['id'],)) == unrelated
    assert c.s.store.all('SELECT id FROM operations') == [{'id': operation_id}]
    assert bool(c.s.store.all('SELECT * FROM conversation_projects WHERE conversation_id=?', (cid,))) is not detach
    account_after = c.s.store.one('SELECT * FROM ' + channel + '_accounts WHERE id=?', (account['account_id'],))
    assert all(account_after[field] == account_before[field] for field in ('secret', 'credential_version', 'generation'))
    before = c.s.store.one('SELECT total_changes() n')['n']
    assert c.s.room_channels.pause_legacy(cid, binding['id'], raw, c.owner) == stopped
    assert c.s.store.one('SELECT total_changes() n')['n'] == before


def test_pre_cutover_successful_flow_receipt_is_readable_but_fresh_creation_is_rejected(collab, monkeypatch):
    c, _, scope, _, _, _, _, _ = legacy_channel(collab, 'weixin')
    raw = {'conversation_id': scope['conversation_id'], 'idempotency_key': key()}
    # Simulate the successful admission made by the preceding application
    # version. Use the real creation/receipt transaction without fabricating
    # room readiness, protocol, bindings or a receipt row.
    with monkeypatch.context() as previous_version:
        previous_version.setattr(c.s.rooms, 'require_native', lambda room: None)
        saved = c.s.room_channels.create(raw, c.owner)
    before = c.s.store.one('SELECT total_changes() n')['n']
    replayed = c.s.room_channels.create(raw, c.owner)
    assert replayed['flow']['id'] == saved['flow']['id']
    assert set(replayed['flow']['allowed_actions']) <= {'pause', 'disconnect', 'cancel', 'revoke_tasks'}
    with pytest.raises(DevError) as fresh:
        c.s.room_channels.create({**raw, 'idempotency_key': key()}, c.owner)
    assert fresh.value.code == 'ROOM_MIGRATION_REQUIRED'
    assert c.s.store.one('SELECT total_changes() n')['n'] == before


@pytest.mark.asyncio
async def test_original_qr_only_flow_closes_after_last_project_is_removed(collab, monkeypatch):
    c = legacy(collab)
    c.s.config = replace(c.s.config, weixin_enabled=True)
    calls = []
    class Provider:
        async def qr_start(self):
            calls.append('start')
            return {'qrcode': 'private-old-qr', 'qrcode_img_content': 'https://weixin.qq.com/synthetic-old-only'}
        async def qr_status(self, *args):
            pytest.fail('Safe cancellation must never poll a provider')
    c.s.weixin.client_factory = lambda *_: Provider()
    create = {'conversation_id': c.conversation_id, 'member_id': c.member['id'], 'idempotency_key': key()}
    # Model a flow committed by the preceding version. Real QR creation and
    # finish_network persist the same provider operation and enrollment ID;
    # only the newly introduced admission guard is absent for that old commit.
    with monkeypatch.context() as previous_version:
        previous_version.setattr(c.s.rooms, 'require_native', lambda room: None)
        flow = c.s.room_channels.create(create, c.owner)['flow']
    qr = await c.s.weixin.qr.start({**c.scope, 'conversation_id': c.conversation_id,
        'confirm_credential_storage': True, 'confirm_provider_terms': True, 'idempotency_key': key()}, c.owner)
    with monkeypatch.context() as previous_version:
        previous_version.setattr(c.s.room_channels, 'write_guard', lambda *args: None)
        flow = c.s.room_channels.finish_network({'flow_id': flow['id'], 'action': 'start_qr',
            'idempotency_key': key()}, c.owner, flow['version'], qr)['flow']
    assert flow['qr']['enrollment_id'] == qr['enrollment_id'] and calls == ['start']
    detached_legacy_partition(c, c.conversation_id)
    with pytest.raises(DevError) as old_cancel:
        c.s.weixin.qr.cancel({**c.scope, 'conversation_id': c.conversation_id,
            'enrollment_id': qr['enrollment_id'], 'expected_version': qr['version'], 'idempotency_key': key()}, c.owner)
    assert old_cancel.value.code == 'CONVERSATION_PROJECT_MISMATCH'
    cancel = {'flow_id': flow['id'], 'action': 'cancel', 'expected_version': flow['version'], 'idempotency_key': key()}
    stopped = await c.s.room_channels.action(cancel, c.owner)
    assert stopped['flow']['stage'] == 'cancelled'
    saved = c.s.store.one('SELECT * FROM weixin_enrollments WHERE id=?', (qr['enrollment_id'],))
    assert saved['state'] == 'cancelled' and saved['payload'] == ''
    before = c.s.store.one('SELECT total_changes() n')['n']
    assert await c.s.room_channels.action(cancel, c.owner) == stopped
    assert c.s.store.one('SELECT total_changes() n')['n'] == before
    assert calls == ['start']
    assert not c.s.store.all('SELECT * FROM weixin_accounts')
    assert not c.s.store.all('SELECT * FROM weixin_bindings')
    remove_original_members(c, c.conversation_id)
    activate(c, c.conversation_id)
    replacement = c.s.room_channels.create({'conversation_id': c.conversation_id, 'idempotency_key': key()}, c.owner)['flow']
    assert replacement['id'] != flow['id'] and replacement['stage'] == 'choose_account'


@pytest.mark.asyncio
@pytest.mark.parametrize('paired', [False, True])
async def test_original_legacy_flow_can_safely_close_after_project_loss(collab, monkeypatch, paired):
    c, transport, scope, account, _ = prepare(collab, 'weixin')
    member = c.s.store.one('SELECT * FROM room_members WHERE legacy_dot_id=?', (c.slot['id'],))
    original_view = c.s.room_channels.view
    def previous_view(row, principal, room):
        value = original_view(row, principal, room)
        if value['stage'] == 'confirm_binding':
            value.update(primary_action='bind', allowed_actions=['bind', 'cancel'])
        return value
    # Reconstruct a previously committed legacy wizard through the real bind
    # and receipt transactions. Only the new M3 admission/display restrictions
    # are absent for this simulated preceding-version commit.
    with monkeypatch.context() as previous_version:
        previous_version.setattr(c.s.rooms, 'require_native', lambda room: None)
        previous_version.setattr(c.s.room_channels, 'write_guard', lambda *args: None)
        previous_version.setattr(c.s.room_channels, 'view', previous_view)
        flow = c.s.room_channels.create({'conversation_id': scope['conversation_id'], 'member_id': member['id'],
            'account_id': account['account_id'], 'idempotency_key': key()}, c.owner)['flow']
        flow = (await c.s.room_channels.action({'flow_id': flow['id'], 'action': 'bind', 'expected_version': flow['version'],
            'confirm_external_data_ingress': True, 'allow_replies': True, 'confirm_until_revoked': True,
            'idempotency_key': key()}, c.owner))['flow']
    binding_id = flow['binding']['id']
    if paired:
        external_send(c, transport, account, incoming(c, 'weixin', 1, flow['binding']['pairing_challenge']))
        binding = transport.binding(binding_id, c.owner, c.room)
        transport.control({**scope, 'binding_id': binding_id, 'expected_version': binding['version'],
            'action': 'activate', 'confirm_peer_id': binding['pending_peer_id'], 'idempotency_key': key()}, c.owner)
    secret = transport.account(account['account_id'], c.owner)['secret']
    detached_legacy_partition(c, scope['conversation_id'])
    flow = c.s.room_channels.read(flow['id'], c.owner)['flow']
    if paired:
        flow = (await c.s.room_channels.action({'flow_id': flow['id'], 'action': 'pause',
            'expected_version': flow['version'], 'idempotency_key': key()}, c.owner))['flow']
        assert flow['binding']['state'] == 'paused'
    close = {'flow_id': flow['id'], 'action': 'disconnect' if paired else 'cancel',
        'expected_version': flow['version'], 'idempotency_key': key()}
    stopped = await c.s.room_channels.action(close, c.owner)
    assert stopped['flow']['stage'] == ('closed' if paired else 'cancelled')
    assert c.s.store.one('SELECT state FROM weixin_bindings WHERE id=?', (binding_id,))['state'] == 'revoked'
    assert transport.account(account['account_id'], c.owner)['secret'] == secret
    before = c.s.store.one('SELECT total_changes() n')['n']
    assert await c.s.room_channels.action(close, c.owner) == stopped
    assert c.s.store.one('SELECT total_changes() n')['n'] == before


def test_legacy_wechat_late_inbound_and_native_helper_cannot_cross_protocol_boundary(collab):
    c, w, scope, account, _, binding, source, _ = legacy_channel(collab, 'weixin')
    cid = scope['conversation_id']
    room = c.s.store.one('SELECT * FROM conversation_rooms WHERE id=?', (cid,))
    member = c.s.store.one('SELECT * FROM room_members WHERE conversation_id=?', (cid,))
    native = c.s.room_channels.native
    for call in (
        lambda: native.bind({'member_id': member['id'], 'account_id': account['account_id']}, {}, c.owner, room),
        lambda: native.approve_tasks(binding['id'], {}, c.owner),
        lambda: native.ingest(binding, w.account(account['account_id'], c.owner), c.owner, room, member, packet(50)['msgs'][0]),
    ):
        before = c.s.store.one('SELECT total_changes() n')['n']
        with pytest.raises(DevError) as blocked:
            with c.s.store.transaction():
                call()
        assert blocked.value.code == 'ROOM_MIGRATION_REQUIRED'
        assert c.s.store.one('SELECT total_changes() n')['n'] == before
    c.s.room_channels.pause_legacy(cid, binding['id'],
        {'channel': 'weixin', 'expected_version': binding['version'], 'idempotency_key': key()}, c.owner)
    remove_original_members(c, cid)
    activate(c, cid)
    before = c.s.store.all('SELECT * FROM collaboration_messages ORDER BY id')
    with pytest.raises(DevError) as stale:
        with c.s.store.transaction():
            # A pre-cutover poll can still carry old in-memory binding facts.
            w.ingest_one(binding, w.account(account['account_id'], c.owner), c.owner,
                c.room, c.s.dots.find(c.slot['id']), packet(50, '迟到批次不能成为新业务。')['msgs'][0])
    assert stale.value.code == 'ROOM_LEGACY_WRITE_DISABLED'
    assert c.s.store.all('SELECT * FROM collaboration_messages ORDER BY id') == before
    assert c.s.store.one('SELECT id FROM collaboration_messages WHERE id=?', (source,))


@pytest.mark.asyncio
async def test_native_channel_after_explicit_cutover_keeps_original_history_and_private_bridge(collab):
    c, w, scope, account, _, binding, source, _ = legacy_channel(collab, 'weixin')
    cid = scope['conversation_id']
    detached_legacy_partition(c, cid)
    data = outbox_evidence(c.s.store)
    secret = w.account(account['account_id'], c.owner)['secret']
    c.s.room_channels.pause_legacy(cid, binding['id'],
        {'channel': 'weixin', 'expected_version': binding['version'], 'idempotency_key': key()}, c.owner)
    remove_original_members(c, cid)
    activate(c, cid)
    # The replacement can chat without resurrecting the removed project. Its
    # explicitly approved private execution adapter uses a different project.
    save_room_permissions(c.s, c.owner, cid, [{'project_id': 'otherproj', 'capabilities': ['read']}])
    seed_grant(c.s.store, 'native-other', 'owner', scopes=('read',), projects=('otherproj',))
    s, owner, actor, clock, identity, _ = await connected(collab,
        scopes=[{'project_id': 'otherproj'}], conversation_id=cid,
        worker_override=replace(c.owner, grant_id='native-other', actor='mcp:native-other:fixture',
            admin=False, projects=['otherproj'], scopes={'read'}))
    assert s.store.one("SELECT 1 FROM room_member_work_bridges WHERE member_id=? AND state='active'", (identity['member_id'],))
    flow = s.room_channels.create({'conversation_id': cid, 'member_id': identity['member_id'],
        'account_id': account['account_id'], 'idempotency_key': key()}, owner)['flow']
    assert flow['stage'] == 'route_conflict' and flow['existing_binding']['id'] == binding['id']
    migration = {'flow_id': flow['id'], 'action': 'migrate', 'expected_version': flow['version'],
        'confirm_migrate_binding_id': binding['id'], 'expected_binding_version': flow['existing_binding']['version'],
        'idempotency_key': key()}
    with pytest.raises(DevError) as stale:
        await s.room_channels.action({**migration, 'expected_binding_version': migration['expected_binding_version'] + 1}, owner)
    assert stale.value.code == 'CHANNEL_ROUTE_CONFLICT'
    flow = (await s.room_channels.action(migration, owner))['flow']
    assert flow['stage'] == 'confirm_binding'
    assert s.store.one('SELECT state FROM weixin_bindings WHERE id=?', (binding['id'],))['state'] == 'revoked'
    assert (await s.room_channels.action(migration, owner))['flow']['id'] == flow['id']
    flow = (await s.room_channels.action({'flow_id': flow['id'], 'action': 'bind',
        'expected_version': flow['version'], 'confirm_external_data_ingress': True,
        'allow_replies': True, 'confirm_until_revoked': True, 'idempotency_key': key()}, owner))['flow']
    s.weixin.commit_updates(s.weixin.claim_poll(account['account_id']),
        packet(100, flow['binding']['pairing_challenge'], create_time_ms=int(clock[0] * 1000) + 1))
    flow = s.room_channels.read(flow['id'], owner)['flow']
    flow = (await s.room_channels.action({'flow_id': flow['id'], 'action': 'confirm_owner',
        'expected_version': flow['version'], 'confirm_peer_id': 'synthetic-peer', 'idempotency_key': key()}, owner))['flow']
    assert flow['stage'] == 'ready' and not flow['work_authorized']
    assert s.weixin.account(account['account_id'], owner)['secret'] == secret
    assert outbox_evidence(s.store) == data
    assert s.channel_owner.current(binding['id'])['state'] == 'revoked'
    assert not s.store.one('''SELECT 1 FROM conversation_projects p JOIN collaboration_rooms r ON r.id=p.room_id
        WHERE p.conversation_id=? AND r.project_id='proj' ''', (cid,))
    assert s.store.one('SELECT id FROM collaboration_messages WHERE id=?', (source,))
    with pytest.raises(DevError) as native_pause:
        s.room_channels.pause_legacy(cid, flow['binding']['id'], {'channel': 'weixin',
            'expected_version': flow['binding']['version'], 'idempotency_key': key()}, owner)
    assert native_pause.value.code == 'LEGACY_CHANNEL_NOT_FOUND'


def test_legacy_pause_is_exact_owner_room_binding_and_closed_shape(collab):
    c, _, scope, _, _, binding, _, _ = legacy_channel(collab, 'weixin')
    raw = {'channel': 'weixin', 'expected_version': binding['version'], 'idempotency_key': key()}
    other = c.s.rooms.create({'title': 'Other room', 'idempotency_key': key()}, c.owner)['room']
    before = c.s.store.one('SELECT total_changes() n')['n']
    for conversation_id, actor, body in (
        (other['id'], c.owner, raw),
        (scope['conversation_id'], c.actor, raw),
        (scope['conversation_id'], c.owner, {**raw, 'action': 'resume'}),
        (scope['conversation_id'], c.owner, {**raw, 'channel': 'unknown'}),
        (scope['conversation_id'], replace(c.owner, user_id='other'), raw),
    ):
        with pytest.raises(DevError):
            c.s.room_channels.pause_legacy(conversation_id, binding['id'], body, actor)
    assert c.s.store.one('SELECT total_changes() n')['n'] == before
