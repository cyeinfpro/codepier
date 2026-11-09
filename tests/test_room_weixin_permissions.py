"""Canonical channel policy inherits safely without upgrading historical inputs."""
import json
from dataclasses import replace

import pytest

from hub.collaboration.common import canonical, digest
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import packet
from tests.test_room_weixin_native import setup_channel


def change_scope(service, owner, identity, capabilities, *, channel=None):
    current = service.room_permissions.read(identity['conversation_id'], owner)
    channels = {name: {'mode': entry['mode'], 'scope': entry['scope']}
        for name, entry in current['channels'].items()}
    if channel is not None:
        channels['weixin'] = channel
    return service.room_permissions.save({
        'conversation_id': identity['conversation_id'], 'expected_version': current['version'],
        'scope': {'project_scopes': [{
            'project_id': 'proj', 'environment_id': 'production', 'capabilities': capabilities,
            'execution_targets': ['project_agent'] if 'execute' in capabilities else [],
        }] if capabilities else []}, 'channels': channels, 'idempotency_key': key(),
    }, owner)


def receive(service, flow, clock, sequence):
    clock[0] += 3
    identifier = service.weixin.commit_updates(service.weixin.claim_poll(flow['account_id']),
        packet(sequence, '请按当前房间权限检查项目', create_time_ms=int(clock[0] * 1000)))['message_ids'][0]
    return service.store.one('SELECT * FROM collaboration_messages WHERE id=?', (identifier,))


async def broad_channel(collab):
    actor = replace(collab[2], grant_id='broad', actor='mcp:broad:fixture',
        scopes={'read', 'write', 'execute'})
    return await setup_channel(collab, tasks=True, worker_override=actor)


async def current_credentials(service, actor, clock, identity):
    clock[0] += 91
    assert service.room_consumers.request_recovery({**identity, 'idempotency_key': key()}, actor)['wakeup_queued']
    await service.events.tick()
    ticket = next(p['data']['wake_ticket'] for p in reversed(service.native_test_callbacks)
        if p['data'].get('member_id') == identity['member_id'])
    session = service.room_consumers.begin({**identity, 'wake_ticket': ticket,
        'consumer_run_id': key(), 'idempotency_key': key()}, actor)
    return {k: session[k] for k in ('consumer_session_id', 'consumer_fence', 'consumer_proof')}


def start_work(service, actor, identity, credentials, source, **extra):
    return service.room_runtime.start_work({
        **identity, **credentials, 'source_message_id': source['id'], 'source_version': source['version'],
        'source_digest': digest(json.loads(source['body'])['body_text']),
        'expected_inbox_revision': service.room_consumers.inbox(identity, actor)['inbox_revision'],
        'acceptance': '按当前房间已批准的能力工作', 'confirm_task': True, 'idempotency_key': key(), **extra,
    }, actor)


@pytest.mark.asyncio
async def test_room_permission_expansion_applies_only_to_new_wechat_inputs(collab):
    (s, owner, actor, clock, identity, _), flow, _, _ = await broad_channel(collab)
    old = receive(s, flow, clock, 2)
    before = s.store.one('SELECT * FROM room_channel_owner_authorizations WHERE binding_id=?', (flow['binding']['id'],))
    assert s.channel_owner.author(old)['authenticated']
    change_scope(s, owner, identity, ['read', 'write', 'execute'])
    current = s.room_channels.read(flow['id'], owner)['flow']
    assert current['permission_mode'] == 'follow_room'
    assert current['permission_member_id'] == identity['member_id']
    assert current['work_authorized']
    assert set(current['task_scope']['projects'][0]['capabilities']) == {'read', 'write', 'execute'}
    assert s.store.one('SELECT * FROM room_channel_owner_authorizations WHERE id=?', (before['id'],)) == before
    assert not s.channel_owner.author(old)['authenticated']
    source = receive(s, flow, clock, 3)
    assert s.channel_owner.author(source)['authenticated']
    saved = s.room_channels.native.input_scope(source, before)
    assert set(saved['projects'][0]['capabilities']) == {'read', 'write', 'execute'}
    assert saved['projects'][0]['execution_targets'] == ['project_agent']
    credentials = await current_credentials(s, actor, clock, identity)
    result = start_work(s, actor, identity, credentials, source)
    goal = s.room_runtime.goal(result['goal_id'])
    spec = s.room_work_bridge.native.record(s.room_work_bridge.native.reference(goal))
    assert set(spec['capabilities']) == {'read', 'write', 'execute'}
    assert not s.store.all('SELECT * FROM operations')


@pytest.mark.asyncio
async def test_room_permission_reduction_and_removal_apply_to_wechat_immediately(collab):
    (s, owner, _, clock, identity, _), flow, _, _ = await broad_channel(collab)
    change_scope(s, owner, identity, ['read', 'write', 'execute'])
    broad = receive(s, flow, clock, 2)
    assert s.channel_owner.author(broad)['authenticated']
    change_scope(s, owner, identity, ['read'])
    assert not s.channel_owner.author(broad)['authenticated']
    restricted = receive(s, flow, clock, 3)
    assert s.channel_owner.author(restricted)['authenticated']
    row = s.store.one('SELECT scope FROM room_channel_owner_inputs WHERE message_id=?', (restricted['id'],))
    assert json.loads(row['scope'])['projects'][0]['capabilities'] == ['read']
    change_scope(s, owner, identity, [])
    assert not s.channel_owner.author(restricted)['authenticated']
    assert not s.channel_owner.author(receive(s, flow, clock, 4))['authenticated']
    assert not s.store.all('SELECT * FROM coordination_goals')


@pytest.mark.asyncio
async def test_legacy_fixed_scope_changes_only_via_canonical_room_policy(collab):
    (s, owner, _, clock, identity, _), flow, _, _ = await broad_channel(collab)
    auth = s.store.one('SELECT * FROM room_channel_owner_authorizations WHERE binding_id=?', (flow['binding']['id'],))
    legacy = json.loads(auth['scope'])
    legacy.pop('scope_mode')
    # Reconstruct a pre-feature database, without granting any new authority.
    s.store.execute('DELETE FROM room_permission_policies WHERE conversation_id=?', (identity['conversation_id'],))
    s.store.execute('DELETE FROM room_permission_revisions WHERE conversation_id=?', (identity['conversation_id'],))
    s.store.execute('UPDATE room_channel_owner_authorizations SET scope=?,scope_digest=? WHERE id=?',
        (canonical(legacy), digest(legacy), auth['id']))
    current = s.room_channels.read(flow['id'], owner)['flow']
    assert current['permission_mode'] == 'fixed'
    assert current['work_authorized'] and current['scope_upgrade_available']
    assert current['primary_action'] == 'refresh'
    assert 'approve_tasks' not in current['allowed_actions']
    old = receive(s, flow, clock, 2)
    assert s.channel_owner.author(old)['authenticated']
    change_scope(s, owner, identity, ['read', 'write', 'execute'])
    upgraded = s.room_channels.read(flow['id'], owner)['flow']
    assert upgraded['permission_mode'] == 'follow_room'
    assert upgraded['work_authorized'] and not upgraded['scope_upgrade_available']
    assert not s.channel_owner.author(old)['authenticated']
    assert s.channel_owner.author(receive(s, flow, clock, 3))['authenticated']


@pytest.mark.asyncio
async def test_wechat_requires_intact_input_scope_without_extra_capabilities(collab):
    (s, owner, _, clock, _, _), flow, _, _ = await broad_channel(collab)
    source = receive(s, flow, clock, 2)
    s.store.execute("UPDATE room_channel_owner_inputs SET scope='{}' WHERE message_id=?", (source['id'],))
    assert not s.channel_owner.author(source)['authenticated']
    assert s.channel_owner.author(receive(s, flow, clock, 3))['authenticated']
    current = s.room_channels.read(flow['id'], owner)['flow']
    assert current['task_scope']['projects'][0]['capabilities'] == ['read']
    assert current['task_scope']['projects'][0]['execution_targets'] == []
    assert not s.store.all('SELECT * FROM operations')


@pytest.mark.asyncio
async def test_custom_wechat_scope_controls_default_work_and_cannot_be_bypassed(collab):
    (s, owner, actor, clock, identity, _), flow, _, _ = await broad_channel(collab)
    change_scope(s, owner, identity, ['read', 'write', 'execute'], channel={
        'mode': 'custom', 'scope': {'project_scopes': [{
            'project_id': 'proj', 'environment_id': 'production', 'capabilities': ['read'],
            'execution_targets': [],
        }]},
    })
    credentials = await current_credentials(s, actor, clock, identity)
    source = receive(s, flow, clock, 2)
    current = s.room_channels.read(flow['id'], owner)['flow']
    assert current['permission_mode'] == 'custom'
    assert current['task_scope']['projects'][0]['capabilities'] == ['read']
    with pytest.raises(DevError) as denied:
        start_work(s, actor, identity, credentials, source, capabilities=['read', 'write'])
    assert denied.value.code == 'CHANNEL_OWNER_SCOPE_DENIED'
    assert not s.store.all('SELECT * FROM coordination_goals')
    result = start_work(s, actor, identity, credentials, source)
    goal = s.room_runtime.goal(result['goal_id'])
    spec = s.room_work_bridge.native.record(s.room_work_bridge.native.reference(goal))
    assert spec['capabilities'] == ['read']
    assert spec['execution_targets'] == []


@pytest.mark.asyncio
async def test_custom_lifetime_extension_does_not_extend_older_input(collab):
    (s, owner, _, clock, identity, _), flow, _, _ = await broad_channel(collab)
    def channel(deadline):
        return {'mode': 'custom', 'scope': {'project_scopes': [{
            'project_id': 'proj', 'environment_id': 'production', 'capabilities': ['read'],
            'execution_targets': [], 'lifetime': {'mode': 'timed', 'expires_at': deadline},
        }]}}
    change_scope(s, owner, identity, ['read'], channel=channel(clock[0] + 30))
    old = receive(s, flow, clock, 2)
    assert s.channel_owner.author(old)['authenticated']
    change_scope(s, owner, identity, ['read'], channel=channel(clock[0] + 300))
    assert not s.channel_owner.author(old)['authenticated']
    assert s.channel_owner.author(receive(s, flow, clock, 3))['authenticated']



@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['remove', 'reduce', 'expire'])
async def test_channel_environment_change_cannot_hide_behind_same_project_other_environment(collab, change):
    (s, owner, actor, clock, identity, _), flow, _, _ = await broad_channel(collab)
    rules = [{
        'project_id': 'proj', 'environment_id': environment,
        'capabilities': ['read', 'write'], 'execution_targets': [],
        'lifetime': {'mode': 'until_revoked', 'expires_at': None},
    } for environment in ('production', 'staging')]
    channel_rules = [dict(rule) for rule in rules]
    if change == 'expire':
        channel_rules[0] = {**channel_rules[0],
            'lifetime': {'mode': 'timed', 'expires_at': clock[0] + 180}}
    current = s.room_permissions.read(identity['conversation_id'], owner)
    s.room_permissions.save({
        'conversation_id': identity['conversation_id'], 'expected_version': current['version'],
        'scope': {'project_scopes': rules},
        'channels': {'weixin': {'mode': 'custom', 'scope': {'project_scopes': channel_rules}},
            'telegram': {'mode': 'follow_room'}}, 'idempotency_key': key(),
    }, owner)
    credentials = await current_credentials(s, actor, clock, identity)
    source = receive(s, flow, clock, 2)
    result = start_work(s, actor, identity, credentials, source,
        project_ids=['proj'], environment_id='production')
    goal = s.room_runtime.goal(result['goal_id'])
    assert s.room_work_bridge.native.owner_author(goal, source)['authenticated']
    if change == 'expire':
        clock[0] += 300
    else:
        current = s.room_permissions.read(identity['conversation_id'], owner)
        narrowed = [channel_rules[1]] if change == 'remove' else [
            {**channel_rules[0], 'capabilities': ['read']}, channel_rules[1]]
        s.room_permissions.save({
            'conversation_id': identity['conversation_id'], 'expected_version': current['version'],
            'scope': current['scope'],
            'channels': {'weixin': {'mode': 'custom', 'scope': {'project_scopes': narrowed}},
                'telegram': {'mode': 'follow_room'}}, 'idempotency_key': key(),
        }, owner)
    # The room/member rule is unchanged; channel validation must independently
    # reject production even though the same project's staging rule survives.
    spec = s.room_work_bridge.native.record(s.room_work_bridge.native.reference(goal))
    lead = spec['members'][0]
    s.room_work_bridge.native.validate_rule(lead, lead['projects'][0], identity['conversation_id'])
    with pytest.raises(DevError) as denied:
        s.room_work_bridge.native.owner_author(goal, source)
    assert denied.value.code == 'CHANNEL_OWNER_SCOPE_CHANGED'



@pytest.mark.asyncio
async def test_custom_channel_selects_its_only_environment_by_default(collab):
    (s, owner, actor, clock, identity, _), flow, _, _ = await broad_channel(collab)
    rules = [{
        'project_id': 'proj', 'environment_id': environment,
        'capabilities': ['read'], 'execution_targets': [],
    } for environment in ('production', 'staging')]
    current = s.room_permissions.read(identity['conversation_id'], owner)
    s.room_permissions.save({
        'conversation_id': identity['conversation_id'], 'expected_version': current['version'],
        'scope': {'project_scopes': rules},
        'channels': {'weixin': {'mode': 'custom', 'scope': {'project_scopes': [rules[1]]}},
            'telegram': {'mode': 'follow_room'}}, 'idempotency_key': key(),
    }, owner)
    credentials = await current_credentials(s, actor, clock, identity)
    source = receive(s, flow, clock, 2)
    with pytest.raises(DevError) as denied:
        start_work(s, actor, identity, credentials, source, environment_id='production')
    assert denied.value.code == 'CHANNEL_OWNER_SCOPE_DENIED'
    result = start_work(s, actor, identity, credentials, source)
    goal = s.room_runtime.goal(result['goal_id'])
    spec = s.room_work_bridge.native.record(s.room_work_bridge.native.reference(goal))
    assert [(r['project_id'], r['environment_id']) for r in spec['projects']] == [('proj', 'staging')]
