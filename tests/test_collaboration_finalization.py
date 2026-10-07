"""Final integration regressions: budgets, truthful routing and distribution."""
from pathlib import Path

import pytest
from shared.util import DevError
from tests.test_collaboration_service import collab, claimed, command, key
from tests.test_collaboration_events import setup_events


def test_reclaim_keeps_job_call_budget_and_refuses_exhausted_budget(collab):
    service, owner, worker, dot, room, agents, clock, scope = collab
    request, lease = claimed(collab)
    service.heartbeat({**scope, 'job_id': lease['id'], 'attempt': lease['attempt'],
                       'fencing_token': lease['fencing_token'], 'idempotency_key': key()}, worker)
    assert service.store.one('SELECT tool_calls FROM collaboration_jobs')['tool_calls'] == 2
    clock[0] += 901
    service.reconcile()
    clock[0] += 31
    service.reconcile()
    current = service.object('collaboration_jobs', room, lease['id'])
    next_lease = service.claim({**request, 'expected_version': current['version'], 'idempotency_key': key()}, worker)
    assert next_lease['tool_calls'] == 3 and next_lease['attempt'] == 2
    service.store.execute('UPDATE collaboration_jobs SET tool_calls=20 WHERE id=?', (lease['id'],))
    blocked = service.block({**scope, 'job_id': lease['id'], 'attempt': next_lease['attempt'],
        'fencing_token': next_lease['fencing_token'], 'reason_code': 'missing_data',
        'summary': 'Need owner review after exhausting the original budget', 'idempotency_key': key()}, worker)
    resumed = service.control({**scope, 'target_id': lease['id'], 'action': 'retry',
        'expected_version': blocked['version'], 'reason': 'Review remaining original allowance', 'idempotency_key': key()}, owner)
    with pytest.raises(DevError) as error:
        service.claim({**request, 'expected_version': resumed['version'], 'idempotency_key': key()}, worker)
    assert error.value.code == 'BUDGET_EXCEEDED'
    assert service.object('collaboration_jobs', room, lease['id'])['attempt'] == 2


@pytest.mark.asyncio
async def test_display_requires_matching_current_subscription(collab):
    service, owner, worker, dot, room, agents, clock, scope = collab
    events, receiver, args = setup_events(collab)
    filtered = {**args, 'arguments': {**args['arguments'], 'severity_min': 'critical'}}
    await events.subscribe(filtered, worker)
    created = command(collab)[1]
    job = service.object('collaboration_jobs', room, created['job_id'])
    assert service.delivery_status(job) == 'no_valid_subscription'
    await events.tick()
    assert len(receiver.requests) == 1  # challenge only, no task matching the filter
    matching = await events.subscribe(args, worker)
    assert service.delivery_status(job) == 'event_queued'
    await events.tick()
    assert service.delivery_status(job) == 'event_accepted'
    row = service.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (matching['id'],))
    service.control({**scope, 'target_id': row['id'], 'expected_version': row['version'],
        'action': 'subscription_pause', 'reason': 'Pause the only matching route', 'idempotency_key': key()}, owner)
    assert service.delivery_status(job) == 'no_valid_subscription'


@pytest.mark.asyncio
async def test_large_requested_subscription_lifetime_is_clamped_before_conversion(collab):
    events, receiver, args = setup_events(collab)
    result = await events.subscribe({**args, 'ttlMs': 10 ** 400}, collab[2])
    row = collab[0].store.one('SELECT expires_at FROM mcp_event_subscriptions WHERE id=?', (result['id'],))
    assert row['expires_at'] == collab[6][0] + 86400


def test_public_source_profile_contains_collaboration_and_reviewed_docs():
    from scripts.build_source_bundle import PUBLIC_DOCS, REQUIRED_FILES, include, ROOT
    for name in ('docs/COLLABORATION.md', 'docs/designs/COLLABORATION_V0_2.md'):
        assert name in PUBLIC_DOCS and name in REQUIRED_FILES
        assert include(Path(name), public=True) and (ROOT / name).is_file()
    for name in ('hub/collaboration/service.py', 'hub/collaboration/events.py',
                 'hub/collaboration/monitor.py', 'shared/collaboration_contracts.py',
                 'web/collaboration.js', 'web/collaboration.css'):
        assert name in REQUIRED_FILES and include(Path(name), public=True)
