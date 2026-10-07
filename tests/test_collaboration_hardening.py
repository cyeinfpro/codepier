"""Focused regressions for conservative recovery, privacy and opt-in defaults."""
import json
from dataclasses import replace

import pytest
from hub.collaboration.common import read_cursor, TASK_EVENT
from hub.collaboration.config import CollaborationConfig
from hub.collaboration.events import EventService
from shared.util import DevError
from tests.test_collaboration_service import collab, key, command
from tests.test_collaboration_monitor import configured, cycle
from tests.test_collaboration_events import setup_events


def test_fast_error_responses_do_not_satisfy_latency_recovery(collab):
    m, probes, candidate, active = configured(collab, dispatch=False)
    s, owner, worker, dot, room, agents, clock, scope = collab
    candidate['rules'][0].update(metric='latency_p95_ms', window_seconds=20,
        open_when={'operator': 'gte', 'value': 500.0}, close_when={'operator': 'lt', 'value': 100.0})
    saved = m.save_plan({**scope, 'candidate': candidate, 'expected_version': 1, 'idempotency_key': key()}, owner)
    m.activate_plan({**scope, 'plan_version': 2, 'expected_version': saved['version'],
                     'digest': saved['digest'], 'idempotency_key': key()}, owner)
    cycle(collab, m, probes, available=True, recovery=True)
    # The measured endpoint now fails quickly while the independent recovery
    # endpoint succeeds. A low p95 must not be mistaken for recovery.
    cycle(collab, m, probes, available=False, recovery=True)
    assert m.plan_view(room)['status'] == 'observing'
    row = s.store.one('SELECT closing FROM monitor_rule_state WHERE plan_version=2')
    assert row['closing'] == 0


@pytest.mark.asyncio
async def test_old_retention_cursor_reports_truncation_and_unsubscribe_is_owned(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    events, receiver, args = setup_events(collab)
    initial = await events.subscribe(args, worker)
    command(collab)
    await events.tick()
    clock[0] += 8 * 86400
    refreshed = await events.subscribe({**args, 'cursor': initial['cursor']}, worker)
    assert refreshed['truncated'] is True
    assert read_cursor(s.secret, refreshed['id'], refreshed['cursor']) > 0
    without_secret = {key: value for key, value in args.items() if key != 'ttlMs'}
    without_secret['delivery'] = {key: value for key, value in args['delivery'].items() if key != 'secret'}
    # Another valid project reader cannot unsubscribe the first grant's identity.
    with pytest.raises(DevError) as missing:
        events.unsubscribe(without_secret, dot)
    assert missing.value.code == 'SUBSCRIPTION_NOT_FOUND'
    assert s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (initial['id'],))['state'] == 'active'
    events.unsubscribe(without_secret, worker)
    assert s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (initial['id'],))['state'] == 'unsubscribed'


@pytest.mark.asyncio
async def test_owner_pause_cannot_be_overridden_by_automatic_renewal(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    events, receiver, args = setup_events(collab)
    subscription = await events.subscribe(args, worker)
    row = s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (subscription['id'],))
    paused = s.control({**scope, 'target_id': row['id'], 'expected_version': row['version'],
                       'action': 'subscription_pause', 'reason': 'Pause this callback', 'idempotency_key': key()}, owner)
    with pytest.raises(DevError) as error:
        await events.subscribe(args, worker)
    assert error.value.code == 'SUBSCRIPTION_PAUSED'
    resumed = s.control({**scope, 'target_id': row['id'], 'expected_version': paused['version'],
                        'action': 'subscription_resume', 'reason': 'Resume this callback', 'idempotency_key': key()}, owner)
    assert resumed['state'] == 'active'


def test_feature_defaults_are_off_and_inconsistent_flags_fail(monkeypatch):
    from shared.config import ConfigurationError
    keys = ('CODEPIER_COLLABORATION_ENABLED', 'CODEPIER_MCP_EVENTS_ENABLED',
            'CODEPIER_MONITOR_COLLECTOR_ENABLED', 'CODEPIER_ANALYSIS_DISPATCH_ENABLED')
    for variable in keys:
        monkeypatch.delenv(variable, raising=False)
    assert CollaborationConfig.from_env() == CollaborationConfig()
    monkeypatch.setenv('CODEPIER_MCP_EVENTS_ENABLED', 'true')
    with pytest.raises(ConfigurationError):
        CollaborationConfig.from_env()
