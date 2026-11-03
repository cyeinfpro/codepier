"""Actual fenced leases under sustained synthetic multi-owner delivery load.

Only the fixture clock and in-process receiver are synthetic. Native enrollment,
callback tickets, probe read/reply, messages, coalescing, budgets and dispatch all
use their production service paths. No send limits are patched in these tests.
"""
import asyncio
import base64
import json
from collections import Counter
from dataclasses import replace

import pytest

from hub.collaboration.common import TASK_EVENT, canonical, digest
from hub.collaboration.events import DISPATCH_BUDGETS, EventService
from hub.collaboration.network import Reply
from hub.principal import Principal
from tests.collaboration_support import collab as collab, key
from tests.legacy_iam_fixture import seed_grant, seed_owner
from tests.test_room_members_rebuild import add, create, identity, join


class NativeRecipients:
    def __init__(self, fixture):
        self.s, _, _, _, _, _, self.clock, _ = fixture
        self.s.config = replace(self.s.config, events_enabled=True)
        self.s.events = EventService(self.s, self.receive)
        self.members, self.subscriptions, self.verified, self.business = {}, {}, set(), []

    async def receive(self, url, body, headers):
        event = json.loads(body)
        if event.get('type') == 'verification':
            return Reply(200, canonical({'challenge': event['challenge']}).encode())
        data = event['data']
        member_id = data['member_id']
        assert url.endswith('/' + member_id)
        host = self.members[member_id]
        assert data['owner_user_id'] == host['owner'].user_id
        assert data['conversation_id'] == host['room']['id']
        assert data['wake_ticket'] and not data['test']
        if member_id not in self.verified:
            def verify():
                probe = self.s.rooms.probe_read({**identity(host['joined']),
                    'wake_ticket': data['wake_ticket'], 'idempotency_key': key()}, host['actor'])
                result = self.s.rooms.probe_reply({**identity(host['joined']),
                    'probe_nonce': probe['probe_nonce'], 'idempotency_key': key()}, host['actor'])
                assert result['member']['state'] == 'ready_chat'
            await self.s.store.run(verify)
            self.verified.add(member_id)
        elif data['dirty_revision']:
            self.business.append((member_id, self.clock[0]))
        return Reply(204)

    async def enroll(self, member_counts):
        for owner_index, count in enumerate(member_counts):
            owner_id, grant_id = f'fair-owner-{owner_index}', f'fair-grant-{owner_index}'
            seed_owner(self.s.store, owner_id, owner_id)
            self.s.store.execute("UPDATE memberships SET level='admin' WHERE user_id=?", (owner_id,))
            seed_grant(self.s.store, grant_id, owner_id, scopes=('read',), projects=())
            owner = Principal('panel:' + owner_id, owner_id, {'read', 'write'}, ['*'], admin=True)
            actor = Principal('mcp:' + grant_id + ':fairness', owner_id, {'read'}, [], grant_id=grant_id)
            for index in range(count):
                if index % 16 == 0:
                    room = create(self.s, owner)
                if index and index % 6 == 0:
                    self.clock[0] += 61  # Honor the unchanged challenge limit.
                joined, _ = join(self.s, actor, add(self.s, owner, room, f'Host {index}'))
                member_id = joined['member_id']
                host = {'joined': joined, 'owner': owner, 'actor': actor, 'room': room}
                self.members[member_id] = host
                result = await self.s.events.subscribe({**joined['subscription_request'], 'delivery': {
                    'mode': 'webhook', 'url': 'https://fairness.example.invalid/' + member_id,
                    'secret': 'whsec_' + base64.b64encode(b'synthetic-fairness-secret-material').decode()}}, actor)
                self.subscriptions[result['id']] = member_id
        for _ in range(len(self.members) * 2 + 8):
            await self.pump(self.s.events)
            if len(self.verified) == len(self.members):
                break
            self.clock[0] += .125
        assert len(self.verified) == len(self.members)
        self.clock[0] += 2
        self.business.clear()

    async def pump(self, events, *, replace_input=False):
        self.s.events = events
        items = events.reserve()
        await asyncio.gather(*(events.deliver(item) for item in items))
        if replace_input:
            for item in items:
                self.send(self.subscriptions[item['subscription_id']])
        return items

    def send(self, member_id):
        host = self.members[member_id]
        return self.s.room_runtime.send({'conversation_id': host['room']['id'],
            'member_id': member_id, 'body_text': 'Synthetic input preserved in the original ledger',
            'client_message_id': key(), 'idempotency_key': key()}, host['owner'])

    def hints(self, step):
        with self.s.store.transaction():
            for member_id in self.members:
                self.s.room_consumers._wake(self.s.rooms.member(member_id), 'synthetic-dirty-' + str(step))

    async def prime(self, pumps):
        for member_id in self.members:
            self.send(member_id)
        # Consume the real initial burst, then discover every outbox page while
        # no token is available. This is precisely where the old page cursor
        # incorrectly treated inspected recipients as if they had been served.
        for _ in range(20):
            await self.pump(pumps[0], replace_input=True)
        for events in pumps:
            for _ in range(20):
                assert events.reserve() == []
                if not events._scan_after:
                    break
        self.business.clear()


@pytest.mark.asyncio
async def test_continuously_dirty_hundred_owners_and_two_hundred_members_get_actual_service(collab):
    recipients = NativeRecipients(collab)
    await recipients.enroll([2] * 100)
    pumps = [EventService(recipients.s, recipients.receive) for _ in range(2)]
    await recipients.prime(pumps)
    started = recipients.clock[0]
    received = Counter()
    last_at = {}
    gaps = []
    # One token per 125ms after the real 16-send burst. Each pump scans a full
    # seven-page inventory between refills, reproducing the old phase lock.
    for step in range(420):
        recipients.clock[0] += .125
        events = pumps[step % 2]
        if step and step % 101 == 0:
            events = pumps[step % 2] = EventService(recipients.s, recipients.receive)
        if step % 17 == 0:
            recipients.hints(step)
        items = await recipients.pump(events, replace_input=True)
        assert len(items) == 1
        for item in items:
            member_id = recipients.subscriptions[item['subscription_id']]
            gaps.append(recipients.clock[0] - last_at.get(member_id, started))
            last_at[member_id] = recipients.clock[0]
            received[member_id] += 1
        before = recipients.s.store.one("SELECT * FROM mcp_event_dispatch_cursor WHERE id='global'")
        for _ in range(6):
            assert events.reserve() == []
        assert recipients.s.store.one("SELECT * FROM mcp_event_dispatch_cursor WHERE id='global'") == before
    assert set(received) == set(recipients.members)
    assert min(received.values()) >= 2
    assert max(gaps) <= 25.25  # 200 connections / the unchanged 8 sends per second.
    assert len(recipients.business) == 420
    assert recipients.s.store.one("SELECT COUNT(*) AS n FROM mcp_event_deliveries WHERE reason_code='mailbox_coalesced'")['n'] > 200
    assert not recipients.s.store.one('SELECT 1 FROM coordination_goals')
    assert DISPATCH_BUDGETS == {'global': (8., 16., 128 * 1024., 256 * 1024.),
                                'owner': (4., 8., 64 * 1024., 128 * 1024.)}


@pytest.mark.asyncio
async def test_hot_owner_subscriptions_cross_pages_without_starving_small_owners(collab):
    recipients = NativeRecipients(collab)
    await recipients.enroll([40] + [1] * 35)
    pumps = [EventService(recipients.s, recipients.receive) for _ in range(2)]
    await recipients.prime(pumps)
    received, owner_received = Counter(), Counter()
    for step in range(40 * 36 + 20):
        recipients.clock[0] += .125
        events = pumps[step % 2]
        if step % 73 == 0:
            recipients.hints(step)
        for item in await recipients.pump(events, replace_input=True):
            member_id = recipients.subscriptions[item['subscription_id']]
            received[member_id] += 1
            owner_received[recipients.members[member_id]['owner'].user_id] += 1
        if len(received) == len(recipients.members):
            break
    assert set(received) == set(recipients.members)
    assert len(owner_received) == 36
    assert max(owner_received.values()) - min(owner_received.values()) <= 2
    assert owner_received['fair-owner-0'] >= 40


@pytest.mark.asyncio
async def test_idle_paused_cooldown_and_owner_budget_do_not_block_other_owners(collab):
    recipients = NativeRecipients(collab)
    await recipients.enroll([2] * 36)
    s = recipients.s
    by_owner = {}
    for member_id, host in recipients.members.items():
        by_owner.setdefault(host['owner'].user_id, []).append(member_id)
    owner_keys = sorted((digest(['legacy', owner]), owner) for owner in by_owner)
    idle, paused, cooldown, limited = [owner for _, owner in owner_keys[:4]]
    for member_id, host in recipients.members.items():
        if host['owner'].user_id != idle:
            recipients.send(member_id)
    pause_host = recipients.members[by_owner[paused][0]]
    room = s.rooms.read({'conversation_id': pause_host['room']['id']}, pause_host['owner'])['room']
    s.rooms.update({'conversation_id': room['id'], 'state': 'paused', 'expected_version': room['version'],
                    'idempotency_key': key()}, pause_host['owner'])
    with s.store.transaction():
        for member_id in by_owner[cooldown]:
            route = s.store.one('''SELECT r.* FROM mcp_event_dispatch_routes r
                JOIN mcp_event_subscriptions s ON s.id=r.subscription_id
                WHERE json_extract(s.arguments,'$.member_id')=?''', (member_id,))
            s.store.execute('INSERT INTO mcp_event_dispatch_endpoints VALUES (?,?,?)',
                            (route['endpoint_key'], recipients.clock[0] + 60, recipients.clock[0]))
        for _ in range(8):
            assert s.events.send_budget(digest(['legacy', limited]), 1000, recipients.clock[0], spend=True)
    events = EventService(s, recipients.receive)
    # The owner's exhausted eight-send burst is not refilled at this clock.
    # Other owners still have the remainder of the real global burst.
    actual = []
    for _ in range(8):
        actual.extend(await recipients.pump(events))
    owners = {recipients.members[recipients.subscriptions[item['subscription_id']]]['owner'].user_id for item in actual}
    assert len(actual) == 8
    assert owners.isdisjoint({idle, paused, cooldown, limited})
    recipients.clock[0] += .25
    for _ in range(80):
        await recipients.pump(events)
        recipients.clock[0] += .125
    seen = {recipients.members[member_id]['owner'].user_id for member_id, _ in recipients.business}
    assert seen == set(by_owner) - {idle, paused, cooldown}


async def disabled_legacy_heads(recipients, owner_id, index, count=1):
    s = recipients.s
    project, grant = 'disabled-project-' + str(index), 'disabled-grant-' + str(index)
    seed_owner(s.store, owner_id, owner_id)
    s.store.execute("UPDATE memberships SET level='admin' WHERE user_id=?", (owner_id,))
    s.store.execute('''INSERT INTO projects
        (id,alias,alias_key,device_id,root,mode,allow_tasks,created,owner_user_id)
        VALUES (?,?,?,'dev','/tmp/fixture','write',1,?,?)''',
        (project, project, project, recipients.clock[0], owner_id))
    seed_grant(s.store, grant, owner_id, scopes=('read',), projects=(project,))
    owner = Principal('panel:' + owner_id, owner_id, {'read', 'write'}, ['*'], admin=True)
    actor = Principal('mcp:' + grant + ':fairness', owner_id, {'read'}, [project], grant_id=grant)
    scope = {'project': project, 'environment_id': 'production'}
    room = s.room_create({**scope, 'idempotency_key': key()}, owner)['room']
    agent = s.register_agent({**scope, 'label': 'Disabled analysis fixture', 'kind': 'work_cloud',
                              'grant_id': grant, 'idempotency_key': key()}, owner)
    for number in range(count):
        await s.events.subscribe({'name': TASK_EVENT,
            'arguments': {'project_id': project, 'environment_id': 'production', 'queue': 'work-analysis'},
            'delivery': {'mode': 'webhook', 'url': f'https://disabled.example.invalid/{owner_id}/{number}',
                        'secret': 'whsec_' + base64.b64encode(b'synthetic-disabled-task-secret!!').decode()}}, actor)
    s.command({**scope, 'room_id': room['id'], 'request': 'Synthetic queued analysis',
        'structured_mentions': [{'agent_id': agent['id']}], 'idempotency_key': key(),
        'source_message_id': key()}, owner, from_panel=True)


@pytest.mark.asyncio
async def test_disabled_legacy_heads_cannot_block_the_next_owner_page_after_restart(collab):
    recipients = NativeRecipients(collab)
    await recipients.enroll([1])
    s = recipients.s
    healthy_id = next(iter(recipients.members))
    healthy_owner = digest(['legacy', recipients.members[healthy_id]['owner'].user_id])
    blocked = [f'disabled-owner-{index}' for index in range(10000)
               if digest(['legacy', f'disabled-owner-{index}']) < healthy_owner][:32]
    assert len(blocked) == 32
    for index, owner_id in enumerate(blocked):
        await disabled_legacy_heads(recipients, owner_id, index)
    s.config = replace(s.config, analysis_dispatch_enabled=False)
    recipients.send(healthy_id)
    with s.store.transaction():
        for budget_owner in ('synthetic-drain-a', 'synthetic-drain-b'):
            for _ in range(8):
                assert s.events.send_budget(budget_owner, 1000, recipients.clock[0], spend=True)
    # Discover pending heads but do not move the actual service cursor while
    # the shared send bucket is empty, regardless of how often a pump scans.
    service_cursor = s.store.one("SELECT after_owner FROM mcp_event_dispatch_cursor WHERE id='global'")
    for _ in range(4):
        assert s.events.reserve() == []
    assert s.store.one("SELECT COUNT(*) AS n FROM mcp_event_deliveries WHERE state='pending'")['n'] == 33
    assert s.store.one("SELECT after_owner FROM mcp_event_dispatch_cursor WHERE id='global'") == service_cursor
    recipients.clock[0] += .125
    assert s.events.reserve() == []
    inspection = s.store.one("SELECT * FROM mcp_event_dispatch_cursor WHERE id='global'")
    assert inspection['after_owner'] == healthy_owner
    assert inspection['scan_after_owner']
    rebuilt = EventService(s, recipients.receive)
    delivered = await recipients.pump(rebuilt)
    assert len(delivered) == 1
    assert recipients.subscriptions[delivered[0]['subscription_id']] == healthy_id
    assert s.store.one("SELECT * FROM mcp_event_dispatch_cursor WHERE id='global'")['scan_after_owner'] is None
    assert s.store.one("SELECT COUNT(*) AS n FROM mcp_event_deliveries WHERE state='pending' AND attempt=0 AND body IS NULL")['n'] == 32


@pytest.mark.asyncio
async def test_one_owners_unavailable_subscriptions_do_not_hide_its_ready_member(collab):
    recipients = NativeRecipients(collab)
    await recipients.enroll([1, 1])
    s = recipients.s
    member_id = next(iter(recipients.members))
    host = recipients.members[member_id]
    await disabled_legacy_heads(recipients, host['owner'].user_id, 0, count=4)
    s.config = replace(s.config, analysis_dispatch_enabled=False)
    recipients.send(member_id)
    owner_key = digest(['legacy', host['owner'].user_id])
    # The previous actual service was this member's connection probe; all
    # four newly eligible legacy heads follow it before the ring wraps back.
    actual_after = s.store.one('SELECT after_subscription FROM mcp_event_dispatch_owners WHERE owner_key=?', (owner_key,))
    assert s.events.reserve() == []
    assert EventService(s, recipients.receive).reserve() == []
    assert s.store.one('SELECT after_subscription FROM mcp_event_dispatch_owners WHERE owner_key=?', (owner_key,)) == actual_after
    rebuilt = EventService(s, recipients.receive)
    items = await recipients.pump(rebuilt)
    assert len(items) == 1 and recipients.subscriptions[items[0]['subscription_id']] == member_id
    assert s.store.one('SELECT after_subscription FROM mcp_event_dispatch_owner_scans WHERE owner_key=?', (owner_key,)) is None
    assert s.store.one("SELECT COUNT(*) AS n FROM mcp_event_deliveries WHERE state='pending' AND attempt=0 AND body IS NULL")['n'] == 4


def test_original_owner_service_journal_keeps_positional_insert_compatibility(collab):
    s = collab[0]
    assert [row['name'] for row in s.store.all('PRAGMA table_info(mcp_event_dispatch_owners)')] == [
        'owner_key', 'after_subscription', 'updated']
    # The released dispatcher uses this exact positional insert after an
    # upgrade that has not yet accepted native Room data.
    s.store.execute('INSERT INTO mcp_event_dispatch_owners VALUES (?,?,?)', ('legacy-owner', 'legacy-sub', s.clock()))
    assert s.store.one('SELECT after_subscription FROM mcp_event_dispatch_owners WHERE owner_key=?',
                       ('legacy-owner',))['after_subscription'] == 'legacy-sub'
