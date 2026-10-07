"""Sealed synthetic probe windows and immutable, independently approved plans."""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from hub.collaboration.common import canonical, INCIDENT_EVENT
from hub.collaboration.monitor import MonitorService
from shared.util import DevError
from tests.test_collaboration_service import collab, key, claimed, result_args, command


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def configured(c, *, dispatch=True, maintenance=None):
    s, owner, worker, dot, room, agents, clock, scope = c
    s.config = replace(s.config, collector_enabled=True, analysis_dispatch_enabled=dispatch)
    clock[0] = int(clock[0] // 10) * 10 + 1
    monitor = MonitorService(s)
    s.monitor = monitor
    probes = [monitor.register_probe({**scope, 'label': label, 'url': 'https://probe.example.invalid/' + label,
              'idempotency_key': key()}, owner)['id'] for label in ('entry', 'recovery')]
    candidate = {'intent': 'Only the registered synthetic probes', 'valid_until': stamp(clock[0] + 3600),
                 'interval_seconds': 10, 'assignee_agent_id': agents[0]['id'],
                 'rules': [{'rule_id': 'available', 'probe_id': probes[0], 'metric': 'availability',
                     'window_seconds': 10, 'min_samples': 1, 'open_when': {'operator': 'lt', 'value': 0.5},
                     'close_when': {'operator': 'gt', 'value': 0.9}, 'open_consecutive_windows': 2,
                     'close_consecutive_windows': 2, 'sample_freshness_seconds': 20,
                     'require_recovery_probe': probes[1], 'cooldown_seconds': 0}]}
    if maintenance:
        candidate['maintenance'] = maintenance(clock[0])
    saved = monitor.save_plan({**scope, 'candidate': candidate, 'expected_version': 0, 'idempotency_key': key()}, owner)
    activated = monitor.activate_plan({**scope, 'plan_version': 1, 'expected_version': saved['version'],
                    'digest': saved['digest'], 'idempotency_key': key()}, owner)
    return monitor, probes, candidate, activated


def cycle(c, monitor, probes, available=False, recovery=False):
    item = monitor.reserve()[0]
    now = c[6][0]
    samples = [{'probe_id': identifier, 'collector_epoch': item['collector_epoch'], 'sequence': item['sequence'],
                'collected_at': now, 'available': value, 'http_error': not value, 'latency_ms': 15.0}
               for identifier, value in zip(probes, (available, recovery))]
    c[6][0] += 10
    accepted = monitor.accept(item, samples)
    assert accepted['accepted']
    return item


def test_plan_draft_not_authority_digest_version_and_immutable_rows(collab):
    monitor, probes, candidate, activated = configured(collab)
    s, owner, worker, dot, room, agents, clock, scope = collab
    with pytest.raises(DevError):
        monitor.activate_plan({**scope, 'plan_version': 1, 'expected_version': activated['version'],
            'digest': '0' * 64, 'idempotency_key': key()}, owner)
    with pytest.raises(DevError):
        monitor.activate_plan({**scope, 'plan_version': 1, 'expected_version': activated['version'],
            'digest': monitor.plan_view(room)['active']['digest'], 'idempotency_key': key()}, worker)
    with pytest.raises(Exception, match='immutable'):
        with s.store.transaction():
            s.store.execute("UPDATE monitor_plans SET config='{}'")
    assert monitor.plan_view(room)['active']['config']['intent'] == candidate['intent']
    assert 'target' not in monitor.probes_view(room)[0]
    assert monitor.probes_view(room, include_targets=True)[0]['target']['url'].startswith('https:')


def test_confirmed_incident_analysis_and_independent_recovery(collab):
    m, probes, candidate, active = configured(collab)
    s, owner, worker, dot, room, agents, clock, scope = collab
    cycle(collab, m, probes)
    assert s.store.all('SELECT * FROM monitor_incidents') == []
    cycle(collab, m, probes)
    incident = s.store.one('SELECT * FROM monitor_incidents')
    assert incident['state'] == 'open' and incident['analysis_state'] == 'queued'
    job = s.store.one('SELECT * FROM collaboration_jobs')
    lease = s.claim({**scope, 'job_id': job['id'], 'expected_version': job['version'], 'idempotency_key': key()}, worker)
    result = result_args(collab, lease, result={'outcome': 'healthy', 'summary': 'A model says restored',
        'observations': [{'claim': 'Probe observations', 'evidence_refs': [incident['evidence_id']]}],
        'recovery': {'verified': True}})
    s.submit(result, worker)
    assert s.object('monitor_incidents', room, incident['id'])['state'] == 'open'
    cycle(collab, m, probes, available=True, recovery=False)
    cycle(collab, m, probes, available=True, recovery=False)
    assert s.object('monitor_incidents', room, incident['id'])['state'] != 'resolved'
    cycle(collab, m, probes, available=True, recovery=True)
    assert m.plan_view(room)['status'] == 'observing'
    cycle(collab, m, probes, available=True, recovery=True)
    assert s.object('monitor_incidents', room, incident['id'])['state'] == 'resolved'
    cycle(collab, m, probes)
    cycle(collab, m, probes)
    reopened = s.object('monitor_incidents', room, incident['id'])
    assert reopened['episode'] == 2 and reopened['state'] == 'open'
    assert len(s.store.all('SELECT * FROM monitor_incidents')) == 1


def test_sealed_window_duplicate_samples_and_stale_never_healthy(collab):
    m, probes, candidate, active = configured(collab, dispatch=False)
    s, owner, worker, dot, room, agents, clock, scope = collab
    item = cycle(collab, m, probes, available=True, recovery=True)
    assert m.accept(item, [])['accepted'] is False
    assert len(s.store.all('SELECT * FROM monitor_samples')) == 2
    binding = s.store.one('SELECT * FROM monitor_plan_bindings')
    plan = m.plan_view(room)['active']['config']
    before = s.store.one('SELECT * FROM monitor_rule_state')
    with s.store.transaction():
        m.evaluate(binding, room, plan)
    assert s.store.one('SELECT * FROM monitor_rule_state') == before
    clock[0] += 100
    with s.store.transaction():
        m.evaluate(binding, room, plan)
    assert m.plan_view(room)['status'] == 'stale'
    assert s.store.one('SELECT closing FROM monitor_rule_state')['closing'] == 0


def test_budget_exhaustion_and_maintenance_preserve_collection_and_deferred_recovery(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    m, probes, candidate, active = configured(collab, maintenance=lambda at: {'timezone': 'Asia/Taipei',
        'windows': [{'starts_at': stamp(at), 'ends_at': stamp(at + 55)}]})
    for available in (False, False, True, True):
        cycle(collab, m, probes, available=available, recovery=available)
    assert s.store.one('SELECT state FROM monitor_incidents')['state'] == 'resolved'
    assert s.store.all('SELECT * FROM collaboration_jobs') == []
    assert s.store.all('SELECT * FROM mcp_event_outbox WHERE name=?', (INCIDENT_EVENT,)) == []
    cycle(collab, m, probes, available=True, recovery=True)
    cycle(collab, m, probes, available=True, recovery=True)
    events = s.store.all('SELECT data FROM mcp_event_outbox WHERE name=?', (INCIDENT_EVENT,))
    assert len(events) == 1 and json.loads(events[0]['data'])['transition'] == 'recovered_after_maintenance'
    command(collab)
    command(collab)
    cycle(collab, m, probes)
    cycle(collab, m, probes)
    assert s.store.one("SELECT analysis_state FROM monitor_incidents WHERE state='open'")['analysis_state'] == 'budget_exhausted'
    assert len(s.store.all('SELECT * FROM collaboration_jobs')) == 2


def test_inflight_plan_replacement_and_pause_invalidate_collection(collab):
    m, probes, candidate, active = configured(collab)
    s, owner, worker, dot, room, agents, clock, scope = collab
    reserved = m.reserve()[0]
    saved = m.save_plan({**scope, 'candidate': {**candidate, 'intent': 'Revised scope'},
                        'expected_version': 1, 'idempotency_key': key()}, owner)
    m.activate_plan({**scope, 'plan_version': 2, 'digest': saved['digest'],
                     'expected_version': saved['version'], 'idempotency_key': key()}, owner)
    assert m.accept(reserved, [])['accepted'] is False
    s.control({**scope, 'action': 'pause', 'target_id': room['id'], 'expected_version': 1,
               'reason': 'Stop new work', 'idempotency_key': key()}, owner)
    assert m.reserve() == []


def test_project_budget_cannot_be_reset_by_environment(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    command(collab)
    command(collab)
    scope2 = {**scope, 'environment_id': 'staging'}
    room2 = s.room_create({**scope2, 'idempotency_key': key()}, owner)['room']
    agent2 = s.register_agent({**scope2, 'label': 'new environment', 'kind': 'work_cloud', 'grant_id': 'worker', 'idempotency_key': key()}, owner)
    with pytest.raises(DevError) as error:
        s.command({**scope2, 'room_id': room2['id'], 'structured_mentions': [{'agent_id': agent2['id']}],
                   'request': 'Try a new environment', 'source_message_id': key(), 'idempotency_key': key()}, owner, from_panel=True)
    assert error.value.code == 'BUDGET_EXCEEDED'
    assert len(s.store.all('SELECT * FROM collaboration_jobs')) == 2
