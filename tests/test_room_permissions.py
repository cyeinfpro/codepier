"""Synthetic coverage for the single room permission authority."""
import copy

import pytest

from shared.util import DevError
from tests.collaboration_support import collab, key  # noqa: F401


def room(collab):
    service, owner, *_ = collab
    result = service.rooms.create({'title': 'permissions', 'projects': [], 'idempotency_key': key()}, owner)
    return service, owner, result['room']['id']


def rule(project='proj', caps=None, expiry=None):
    caps = caps or ['read']
    return {'project_id': project, 'environment_id': 'production', 'capabilities': caps,
            'execution_targets': ['project_agent'] if 'execute' in caps else [],
            'lifetime': {'mode': 'timed' if expiry is not None else 'until_revoked', 'expires_at': expiry}}


def save(service, owner, cid, rules, channels=None, version=None):
    return service.room_permissions.save({'conversation_id': cid,
        'expected_version': service.room_permissions.read(cid, owner)['version'] if version is None else version,
        'scope': {'project_scopes': rules}, 'channels': channels or {
            'weixin': {'mode': 'follow_room'}, 'telegram': {'mode': 'follow_room'}},
        'idempotency_key': key()}, owner)


def member(service, owner, cid):
    return service.rooms.create_member({'conversation_id': cid, 'name': 'worker', 'idempotency_key': key()}, owner)['member']


def test_empty_preview_does_not_expand_or_write(collab):
    service, owner, cid = room(collab)
    preview = service.room_permissions.read(cid, owner)
    assert preview['version'] == 0 and not preview['configured']
    assert preview['scope']['project_scopes'] == []
    assert preview['available_projects']
    assert not service.room_permissions.is_configured(cid)
    assert not service.store.all('SELECT * FROM room_permission_policies')


def test_canonical_follow_custom_subset_and_stale_version(collab):
    service, owner, cid = room(collab)
    full = rule(caps=['read', 'write', 'execute'])
    result = save(service, owner, cid, [full])
    assert result['version'] == 1
    assert service.room_permissions.effective_scopes(cid, 'weixin') == [full | {'capabilities': ['execute', 'read', 'write']}]
    assert result['channels']['telegram']['transport_supported'] is False
    custom = {'weixin': {'mode': 'custom', 'scope': {'project_scopes': [rule()]}},
              'telegram': {'mode': 'follow_room'}}
    save(service, owner, cid, [full], custom)
    assert service.room_permissions.channel_mode(cid, 'weixin') == 'custom'
    assert service.room_permissions.effective_scopes(cid, 'weixin')[0]['capabilities'] == ['read']
    with pytest.raises(DevError, match='权限已更新'):
        save(service, owner, cid, [full], version=0)
    with pytest.raises(DevError, match='渠道权限'):
        save(service, owner, cid, [rule()], {
            'weixin': {'mode': 'custom', 'scope': {'project_scopes': [full]}},
            'telegram': {'mode': 'follow_room'}})
    assert service.room_permissions.read(cid, owner)['version'] == 2


def test_only_room_page_controls_native_members_and_projects(collab):
    service, owner, cid = room(collab)
    with pytest.raises(DevError, match='房间'):
        member(service, owner, cid)
    save(service, owner, cid, [rule(caps=['read', 'write'])])
    m = member(service, owner, cid)
    scopes = service.rooms.scopes(service.rooms.member(m['id']))
    assert scopes[0]['capabilities'] == ['read', 'write']
    with pytest.raises(DevError, match='房间'):
        service.rooms.create_member({'conversation_id': cid, 'name': 'independent',
            'project_scopes': [], 'idempotency_key': key()}, owner)
    with pytest.raises(DevError, match='房间'):
        service.rooms.projects({'conversation_id': cid, 'projects': [],
            'expected_version': service.rooms.read({'conversation_id': cid}, owner)['room']['version'],
            'idempotency_key': key()}, owner)


def test_preserves_unchanged_timed_approval_and_revokes_removed_project(collab):
    service, owner, cid = room(collab)
    expiry = collab[6][0] + 100
    first = rule(expiry=expiry)
    save(service, owner, cid, [first])
    m = member(service, owner, cid)
    original = service.store.one('SELECT * FROM room_member_scopes WHERE member_id=?', (m['id'],))
    save(service, owner, cid, [first, rule('otherproj')])
    current = service.store.one('SELECT * FROM room_member_scopes WHERE member_id=? AND project_id=?', (m['id'], 'proj'))
    assert current == original
    save(service, owner, cid, [first])
    current = service.store.one('SELECT * FROM room_member_scopes WHERE member_id=? AND project_id=?', (m['id'], 'proj'))
    assert current == original
    removed = service.store.one('SELECT * FROM room_member_scopes WHERE member_id=? AND project_id=?', (m['id'], 'otherproj'))
    assert removed['state'] == 'detached'
    collab[6][0] = expiry + 1
    assert service.room_permissions.effective_scopes(cid, 'weixin') == []


def test_migration_preview_keeps_actual_readonly_not_host_maximum(collab):
    service, owner, cid = room(collab)
    save(service, owner, cid, [rule()])
    m = member(service, owner, cid)
    # Simulate persisted pre-policy data; this is fixture construction only.
    service.store.execute('DELETE FROM room_permission_policies WHERE conversation_id=?', (cid,))
    before = copy.deepcopy(service.rooms.scopes(service.rooms.member(m['id'])))
    preview = service.room_permissions.read(cid, owner)
    assert preview['scope']['project_scopes'][0]['capabilities'] == ['read']
    assert preview['available_projects'][0]['capabilities'] != []
    assert service.rooms.scopes(service.rooms.member(m['id'])) == before
    assert not service.room_permissions.is_configured(cid)


def test_project_ceiling_and_channel_expiry_atomicity(collab):
    service, owner, cid = room(collab)
    save(service, owner, cid, [rule()])
    service.store.execute("UPDATE projects SET mode='read' WHERE id='proj'")
    with pytest.raises(DevError, match='仅允许读取'):
        save(service, owner, cid, [rule(caps=['read', 'write'])])
    assert service.room_permissions.read(cid, owner)['version'] == 1
    expiry = collab[6][0] + 100
    with pytest.raises(DevError, match='渠道权限'):
        save(service, owner, cid, [rule(expiry=expiry)], {
            'weixin': {'mode': 'custom', 'scope': {'project_scopes': [rule()]}},
            'telegram': {'mode': 'follow_room'}})
    assert service.room_permissions.read(cid, owner)['version'] == 1


def test_replay_is_once_and_sync_failure_rolls_back(collab, monkeypatch):
    service, owner, cid = room(collab)
    request = {'conversation_id': cid, 'expected_version': 0,
        'scope': {'project_scopes': [rule()]}, 'channels': {
            'weixin': {'mode': 'follow_room'}, 'telegram': {'mode': 'follow_room'}},
        'idempotency_key': key()}
    service.room_permissions.save(request, owner)
    service.room_permissions.save(request, owner)
    assert service.room_permissions.read(cid, owner)['version'] == 1
    assert len(service.store.all('SELECT * FROM room_permission_revisions WHERE conversation_id=?', (cid,))) == 1
    m = member(service, owner, cid)
    before = service.store.one('SELECT * FROM room_member_scopes WHERE member_id=?', (m['id'],))
    def interrupted(*args):
        raise RuntimeError('synthetic interruption')
    monkeypatch.setattr(service.room_permissions, 'sync_member', interrupted)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        save(service, owner, cid, [rule('otherproj')])
    assert service.room_permissions.read(cid, owner)['version'] == 1
    assert service.store.one('SELECT * FROM room_member_scopes WHERE member_id=?', (m['id'],)) == before
    assert service.room_permissions.effective_scopes(cid)[0]['project_id'] == 'proj'


def test_worker_cannot_mutate_and_grants_are_unchanged(collab):
    service, owner, cid = room(collab)
    before = service.store.all('SELECT * FROM grants ORDER BY id')
    request = {'conversation_id': cid, 'expected_version': 0,
        'scope': {'project_scopes': [rule(caps=['read', 'write', 'execute'])]},
        'channels': {'weixin': {'mode': 'follow_room'}, 'telegram': {'mode': 'follow_room'}}}
    with pytest.raises(DevError):
        service.room_permissions.save(request, collab[2])
    assert not service.room_permissions.is_configured(cid)
    service.room_permissions.save(request, owner)
    assert service.store.all('SELECT * FROM grants ORDER BY id') == before


def test_member_create_replays_original_identity_after_policy_change_and_expiry(collab):
    service, owner, cid = room(collab)
    save(service, owner, cid, [rule()])
    request = {'conversation_id': cid, 'name': 'retry-safe', 'idempotency_key': key(),
        'expected_version': service.rooms.read({'conversation_id': cid}, owner)['room']['version']}
    original = service.rooms.create_member(request, owner)
    flow = service.store.one('SELECT id FROM room_connect_flows WHERE member_id=?', (original['member']['id'],))
    expiry = collab[6][0] + 30
    save(service, owner, cid, [rule(caps=['read', 'write'], expiry=expiry)])
    replay = service.rooms.create_member(request, owner)
    assert replay['member']['id'] == original['member']['id']
    collab[6][0] = expiry + 1
    replay = service.rooms.create_member(request, owner)
    assert replay['member']['id'] == original['member']['id']
    assert service.store.one('SELECT id FROM room_connect_flows WHERE member_id=?', (original['member']['id'],)) == flow
    assert service.store.one('SELECT COUNT(*) n FROM room_members WHERE conversation_id=?', (cid,))['n'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['revoked', 'expired'])
async def test_unavailable_member_grant_does_not_block_owner_policy_reduction(collab, unavailable):
    import time
    from tests.test_room_managed_work import connected
    service, owner, worker, _, first, _ = await connected(collab, scopes=[rule(caps=['read', 'write'])])
    cid = first['conversation_id']
    _, _, _, _, second, _ = await connected(collab, conversation_id=cid, worker_override=collab[3])
    if unavailable == 'revoked':
        service.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (worker.grant_id,))
    else:
        service.store.execute('INSERT OR REPLACE INTO grant_expirations(grant_id,expires) VALUES (?,?)',
            (worker.grant_id, time.time() - 1))
    with pytest.raises(DevError):
        service.rooms.principal(service.rooms.member(first['member_id']))
    result = save(service, owner, cid, [rule()])
    assert result['scope']['project_scopes'][0]['capabilities'] == ['read']
    assert service.rooms.state(service.rooms.member(first['member_id'])) != 'ready'
    assert not service.store.all("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'",
        (first['member_id'],))
    assert service.store.all("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'",
        (second['member_id'],))
    with pytest.raises(DevError):
        service.rooms.principal(service.rooms.member(first['member_id']))
