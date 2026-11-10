"""Server rollout policy is versioned and never becomes project authority."""
import json
import logging
from unittest.mock import patch

import pytest

from hub.collaboration.room_turn_policy import record_shadow
from hub.unified_help import action_help
from shared.public_collaboration import action_help as collaboration_help
from shared.unified_contracts import ACTIONS, resolve_help
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_consumer_sessions import begin, connect, credentials, verify
from tests.test_room_members_rebuild import create, identity, rooms as rooms


def test_turn_policy_override_cas_idempotency_and_live_admin(collab, monkeypatch):
    service, owner, worker, *_ = collab
    room = create(service, owner)
    other = create(service, owner)
    monkeypatch.setenv('CODEPIER_ROOM_CHAT_SCOPE_MODE', 'shadow')
    identity = {'conversation_id': room['id']}
    raw = {**identity, 'mode': 'decoupled', 'expected_version': 0,
           'reason': 'isolated verification', 'idempotency_key': key()}
    before = service.rooms.turn_policy(identity, owner)
    assert (before['mode'], before['version']) == ('shadow', 0)
    assert not before['grants_authority']
    with pytest.raises(DevError) as denied:
        service.rooms.update_turn_policy(raw, worker)
    assert denied.value.code == 'OWNER_REQUIRED'
    first = service.invoke('collaboration', {'action': 'room_turn_policy_update', **raw}, owner)
    assert (first['mode'], first['version'], first['applies_to']) == ('decoupled', 1, 'new_turns')
    assert service.rooms.update_turn_policy(raw, owner) == first
    assert service.rooms.turn_policy({'conversation_id': other['id']}, owner)['mode'] == 'shadow'
    with pytest.raises(DevError) as conflict:
        service.rooms.update_turn_policy({**raw, 'mode': 'strict'}, owner)
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    with pytest.raises(DevError) as stale:
        service.rooms.update_turn_policy({**raw, 'idempotency_key': key()}, owner)
    assert stale.value.code == 'ROOM_TURN_POLICY_CHANGED'
    rolled_back = service.rooms.update_turn_policy(
        {**raw, 'mode': None, 'expected_version': 1, 'idempotency_key': key()}, owner)
    assert (rolled_back['mode'], rolled_back['override'], rolled_back['version']) == ('shadow', None, 2)
    audit = service.store.all("SELECT detail FROM audit WHERE action='room.turn_policy' AND target=? ORDER BY id",
                              (room['id'],))
    assert [(entry['previous_mode'], entry['mode'], entry['reason']) for entry in
            (json.loads(row['detail']) for row in audit)] == [
                ('shadow', 'decoupled', raw['reason']), ('decoupled', 'shadow', raw['reason'])]
    service.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id=?", (owner.user_id,))
    with pytest.raises(DevError) as no_longer_admin:
        service.rooms.update_turn_policy({**raw, 'expected_version': 2, 'idempotency_key': key()}, owner)
    assert no_longer_admin.value.code == 'INSTANCE_ADMIN_REQUIRED'


def test_room_help_declares_exact_turn_capabilities():
    address = resolve_help('collab.send', 'room_reply', 'act', visible=ACTIONS)
    help_view = action_help(address, ACTIONS['act']['collab.send'], detail='schema')
    assert help_view['turn_capabilities'] == {'explicit_publication': True, 'reply_yield_after': True}
    arguments = help_view['inputSchema']['properties']['args']['properties']
    assert {'publish_work', 'yield_after'} <= arguments.keys()
    inbox = collaboration_help('collaboration_query', 'room_inbox')
    assert inbox['turn_capabilities'] == {'expired_route_session_read': True}
    assert {'consumer_session_id', 'consumer_fence', 'consumer_proof'} <= inbox['inputSchema']['properties'].keys()


def test_shadow_telemetry_uses_only_fixed_dimensions_and_cannot_break_decisions(caplog):
    with caplog.at_level(logging.INFO, logger='hub.collaboration.room_turn_policy'):
        record_shadow('reply', 'would_allow', 'scope_changed')
        record_shadow('reply', 'would_allow', 'sensitive arbitrary content')
    [entry] = caplog.records
    assert (entry.action_class, entry.decision_class, entry.reason_code, entry.policy_revision) == (
        'reply', 'would_allow', 'scope_changed', 1)
    with patch('hub.collaboration.room_turn_policy._LOG.info', side_effect=RuntimeError):
        record_shadow('reply', 'would_allow', 'scope_changed')


@pytest.mark.asyncio
async def test_context_expiry_requires_the_original_fenced_turn_and_fixed_policy(rooms):
    c = await connect(rooms)
    verify(c)
    identity_args = identity(c.joined)
    c.s.rooms.update_turn_policy({'conversation_id': c.room['id'], 'mode': 'decoupled',
                                  'expected_version': 0, 'reason': 'exercise bounded read grace',
                                  'idempotency_key': key()}, c.owner)
    c.s.store.execute('UPDATE mcp_event_subscriptions SET expires_at=? WHERE id=?',
                      (c.clock[0] + 1, c.sub['id']))
    _, turn = begin(c)
    assert turn['turn_policy'] == 'decoupled'
    assert c.s.room_context.read(identity_args, c.actor)['conversation_id'] == c.room['id']
    c.s.rooms.update_turn_policy({'conversation_id': c.room['id'], 'mode': 'strict',
                                  'expected_version': 1, 'reason': 'stop new decoupled turns',
                                  'idempotency_key': key()}, c.owner)
    c.clock[0] += 2
    with pytest.raises(DevError) as inactive:
        c.s.room_context.read(identity_args, c.actor)
    assert inactive.value.code == 'ROOM_SUBSCRIPTION_INACTIVE'
    assert c.s.room_context.read(credentials(c, turn), c.actor)['conversation_id'] == c.room['id']
    with pytest.raises(DevError) as incomplete:
        c.s.room_context.read({**identity_args, 'consumer_fence': turn['consumer_fence']}, c.actor)
    assert incomplete.value.code == 'INVALID_ARGUMENTS'
    with pytest.raises(DevError) as wrong:
        c.s.room_context.read({**credentials(c, turn), 'consumer_proof': 'x' * 40}, c.actor)
    assert wrong.value.code == 'DOT_CONSUMER_PROOF_REQUIRED'
    diagnostics = c.s.rooms.diagnostics({'conversation_id': c.room['id']}, c.owner)
    assert diagnostics['turn_policy']['mode'] == 'strict'
    assert diagnostics['members'][0]['consumer']['turn_policy'] == 'decoupled'
    assert diagnostics['members'][0]['current_turn_wake']['notification_delivery_id']
