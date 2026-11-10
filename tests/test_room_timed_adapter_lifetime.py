"""Native room approval deadlines, not legacy adapter defaults, govern adapters."""
import pytest

from shared.util import DevError
from tests.collaboration_support import collab  # noqa: F401
from tests.test_room_managed_work import connected
from tests.test_room_permissions import rule, save


@pytest.mark.asyncio
@pytest.mark.parametrize('days', [3, 30])
async def test_native_timed_adapter_honors_explicit_room_deadline(collab, days):
    deadline = collab[6][0] + days * 86400
    service, owner, _, clock, identity, _ = await connected(collab, scopes=[rule(expiry=deadline)])
    member = service.rooms.member(identity['member_id'])
    bridge = service.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'", (member['id'],))
    slot = service.store.one('SELECT * FROM collaboration_join_slots WHERE id=?', (bridge['legacy_dot_id'],))
    policy = service.store.one('SELECT * FROM delegation_policies WHERE id=?', (bridge['policy_id'],))
    assert slot['expires_at'] == deadline
    assert policy['expires_at'] == deadline
    clock[0] = deadline - 1
    assert service.rooms.member_scope_authorized(service.store.one('SELECT * FROM room_member_scopes WHERE member_id=?', (member['id'],)))
    assert not service.channel_owner.expired(slot)
    assert not service.channel_owner.expired(policy)
    clock[0] = deadline
    assert service.channel_owner.expired(slot)
    assert service.channel_owner.expired(policy)
    with pytest.raises(DevError):
        save(service, owner, identity['conversation_id'], [rule(expiry=deadline)])


@pytest.mark.asyncio
async def test_timed_deadline_changes_preserve_grant_and_stop_at_new_limit(collab):
    start = collab[6][0]
    service, owner, worker, clock, identity, _ = await connected(collab, scopes=[rule(expiry=start + 3 * 86400)])
    cid = identity['conversation_id']
    grant_before = service.store.one('SELECT * FROM grants WHERE id=?', (worker.grant_id,))
    grant_expiry_before = service.store.one('SELECT * FROM grant_expirations WHERE grant_id=?', (worker.grant_id,))
    for days in (30, 2):
        deadline = start + days * 86400
        save(service, owner, cid, [rule(expiry=deadline)])
        bridge = service.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'", (identity['member_id'],))
        for table, identifier in [('collaboration_join_slots', bridge['legacy_dot_id']), ('delegation_policies', bridge['policy_id'])]:
            record = service.store.one('SELECT * FROM ' + table + ' WHERE id=?', (identifier,))
            assert record['expires_at'] == deadline
            assert not service.channel_owner.expired(record)
    assert service.store.one('SELECT * FROM grants WHERE id=?', (worker.grant_id,)) == grant_before
    assert service.store.one('SELECT * FROM grant_expirations WHERE grant_id=?', (worker.grant_id,)) == grant_expiry_before
    clock[0] = start + 2 * 86400
    assert service.channel_owner.expired(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["revoked", "expired"])
async def test_timed_room_approval_cannot_revive_unavailable_grant(collab, unavailable):
    service, owner, worker, clock, identity, _ = await connected(collab, scopes=[rule(expiry=collab[6][0] + 30 * 86400)])
    if unavailable == 'revoked':
        service.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (worker.grant_id,))
    else:
        import time
        service.store.execute('INSERT OR REPLACE INTO grant_expirations(grant_id,expires) VALUES (?,?)',
                              (worker.grant_id, time.time() - 1))
    save(service, owner, identity['conversation_id'], [rule(expiry=clock[0] + 40 * 86400)])
    with pytest.raises(DevError):
        service.rooms.principal(service.rooms.member(identity['member_id']))
    if unavailable == 'revoked':
        assert service.store.one('SELECT revoked FROM grants WHERE id=?', (worker.grant_id,))['revoked'] == 1
    else:
        assert service.store.one('SELECT expires FROM grant_expirations WHERE grant_id=?', (worker.grant_id,))['expires'] < time.time()


@pytest.mark.asyncio
async def test_old_default_truncation_recovers_only_within_original_room_deadline(collab):
    start = collab[6][0]
    deadline = start + 30 * 86400
    service, owner, _, clock, identity, _ = await connected(collab, scopes=[rule(expiry=deadline)])
    bridge = service.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'", (identity['member_id'],))
    for table, identifier in [('collaboration_join_slots', bridge['legacy_dot_id']), ('delegation_policies', bridge['policy_id'])]:
        service.store.execute('UPDATE ' + table + ' SET expires_at=? WHERE id=?', (start + 7 * 86400, identifier))
    clock[0] = start + 8 * 86400
    result = save(service, owner, identity['conversation_id'], [rule(expiry=deadline)])
    assert result['scope']['project_scopes'][0]['lifetime']['expires_at'] == deadline
    for table, identifier in [('collaboration_join_slots', bridge['legacy_dot_id']), ('delegation_policies', bridge['policy_id'])]:
        record = service.store.one('SELECT * FROM ' + table + ' WHERE id=?', (identifier,))
        assert record['expires_at'] == deadline
        assert not service.channel_owner.expired(record)
    clock[0] = deadline
    with pytest.raises(DevError):
        save(service, owner, identity['conversation_id'], [rule(expiry=deadline)])
    assert service.channel_owner.expired(record)


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', ['slot', 'member'])
async def test_native_deadline_sync_never_revives_revoked_binding(collab, revoked):
    start = collab[6][0]
    deadline = start + 30 * 86400
    service, owner, _, clock, identity, _ = await connected(collab, scopes=[rule(expiry=deadline)])
    bridge = service.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'", (identity['member_id'],))
    if revoked == 'slot':
        service.store.execute("UPDATE collaboration_join_slots SET state='revoked',expires_at=? WHERE id=?", (start + 7 * 86400, bridge['legacy_dot_id']))
    else:
        service.store.execute("UPDATE room_members SET status='removed' WHERE id=?", (identity['member_id'],))
    clock[0] = start + 8 * 86400
    if revoked == 'slot':
        with pytest.raises(DevError) as error:
            save(service, owner, identity['conversation_id'], [rule(expiry=deadline)])
        assert error.value.code == 'JOIN_SLOT_REVOKED'
        assert service.store.one('SELECT state FROM collaboration_join_slots WHERE id=?', (bridge['legacy_dot_id'],))['state'] == 'revoked'
    else:
        save(service, owner, identity['conversation_id'], [rule(expiry=deadline)])
        assert service.rooms.member(identity['member_id'])['status'] == 'removed'
    slot = service.store.one('SELECT * FROM collaboration_join_slots WHERE id=?', (bridge['legacy_dot_id'],))
    assert service.channel_owner.expired(slot)
