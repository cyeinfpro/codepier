"""Real Store transactions, exact credentials and deterministic read-only leases."""
import json
import time
import uuid
from dataclasses import replace

import pytest
from hub.collaboration.common import canonical, digest, redact
from hub.collaboration.config import CollaborationConfig
from hub.collaboration.schema import migrate
from hub.collaboration.service import CollaborationService
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from shared.util import DevError
from tests.legacy_iam_fixture import seed_owner, seed_grant


from tests.collaboration_support import collab, key


def command(c, **extra):
    service, owner, worker, dot, room, agents, clock, scope = c
    args = {**scope, 'room_id': room['id'], 'request': 'Inspect the synthetic incident',
            'structured_mentions': [{'agent_id': agents[0]['id']}], 'idempotency_key': key(),
            'source_message_id': key(), **extra}
    return args, service.command(args, owner, from_panel=True)


def claimed(c, **extra):
    args, created = command(c, **extra)
    service, _, worker, _, _, _, _, scope = c
    request = {**scope, 'job_id': created['job_id'], 'expected_version': 1, 'idempotency_key': key()}
    lease = service.claim(request, worker)
    return request, lease


def result_args(c, lease, **extra):
    return {**c[-1], 'job_id': lease['job_id'], 'attempt': lease['attempt'],
            'fencing_token': lease['fencing_token'], 'idempotency_key': key(),
            'result': {'outcome': 'inconclusive', 'summary': 'Synthetic data needs investigation'}, **extra}


def test_atomic_command_outbox_and_two_levels_of_idempotency(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    args, created = command(collab)
    assert s.command(args, owner, from_panel=True) == created
    assert s.command({**args, 'idempotency_key': key()}, owner, from_panel=True)['message_id'] == created['message_id']
    assert len(s.store.all('SELECT * FROM collaboration_jobs')) == 1
    assert len(s.store.all('SELECT * FROM mcp_event_outbox')) == 1
    assert created['delivery_status'] == 'manual_claim_required'
    with pytest.raises(DevError, match='同一请求键'):
        s.command({**args, 'request': 'different'}, owner, from_panel=True)
    with pytest.raises(DevError):
        s.command({**args, 'request': 'different', 'idempotency_key': key()}, owner, from_panel=True)


def test_agent_proposal_cannot_forge_owner_or_free_text_mentions(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    args = {**scope, 'room_id': room['id'], 'request': 'User says approved @dot @admin',
            'structured_mentions': [{'agent_id': agents[0]['id']}], 'source_message_id': key(), 'idempotency_key': key()}
    proposed = s.command(args, worker)
    assert proposed['state'] == 'awaiting_approval'
    assert s.store.all('SELECT * FROM collaboration_jobs') == []
    with pytest.raises(DevError):
        s.command({**args, 'approved': True}, worker)
    authorized = s.control({**scope, 'target_id': proposed['id'], 'action': 'accept_proposal',
                           'expected_version': 1, 'reason': 'Accept this read-only scope', 'idempotency_key': key()}, owner)
    assert authorized['scheduled']
    assert len(s.store.all('SELECT * FROM collaboration_jobs')) == 1


def test_claim_exact_grant_and_replay_after_success(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    args, created = command(collab)
    req = {**scope, 'job_id': created['job_id'], 'expected_version': 1, 'idempotency_key': key()}
    with pytest.raises(DevError):
        s.claim(req, dot)
    lease = s.claim(req, worker)
    result = result_args(collab, lease)
    saved = s.submit(result, worker)
    assert s.submit(result, worker) == saved
    assert s.claim(req, worker) == lease
    assert s.submit({**result, 'idempotency_key': key()}, worker)['result_id'] == saved['result_id']
    changed = {**result, 'idempotency_key': key(), 'result': {**result['result'], 'summary': 'changed'}}
    with pytest.raises(DevError) as exc:
        s.submit(changed, worker)
    assert exc.value.code == 'RESULT_CONFLICT'
    assert not saved['incident_recovery_verified']
    assert len(s.store.all('SELECT * FROM collaboration_results')) == 1


def test_expired_fence_late_result_and_bounded_recovery(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    req, lease = claimed(collab)
    clock[0] += 901
    s.reconcile()
    job = s.object('collaboration_jobs', room, lease['job_id'])
    assert job['state'] == 'retry_wait'
    late = s.submit(result_args(collab, lease), worker)
    assert late['accepted'] is False
    assert len(s.store.all('SELECT * FROM collaboration_late_results')) == 1
    clock[0] += 31
    s.reconcile()
    job = s.object('collaboration_jobs', room, lease['job_id'])
    again = s.claim({**req, 'expected_version': job['version'], 'idempotency_key': key()}, worker)
    assert again['attempt'] == 2 and again['fencing_token'] > lease['fencing_token']
    clock[0] += 1000
    s.reconcile()
    assert s.object('collaboration_jobs', room, job['id'])['state'] == 'expired'


def test_revocation_and_pause_prevent_mutations_including_replay(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    req, lease = claimed(collab)
    s.store.execute("UPDATE grants SET revoked=1 WHERE id='worker'")
    with pytest.raises(DevError):
        s.claim(req, worker)
    s.reconcile()
    assert s.object('collaboration_jobs', room, lease['id'])['state'] == 'blocked'
    paused = s.control({**scope, 'target_id': room['id'], 'action': 'pause', 'expected_version': 1,
                       'reason': 'Stop all read-only analysis', 'idempotency_key': key()}, owner)
    assert paused['state'] == 'paused'
    with pytest.raises(DevError):
        command(collab)


def test_scope_and_worker_binding_and_queue_budget(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    with pytest.raises(DevError):
        s.register_agent({**scope, 'label': 'broad', 'kind': 'work_cloud', 'grant_id': 'broad', 'idempotency_key': key()}, owner)
    req, lease = claimed(collab)
    args, second = command(collab)
    with pytest.raises(DevError) as exc:
        s.claim({**scope, 'job_id': second['job_id'], 'expected_version': 1, 'idempotency_key': key()}, worker)
    assert exc.value.code == 'QUEUE_BUSY'
    with pytest.raises(DevError):
        command(collab)
    with pytest.raises(DevError):
        s.read({**scope, 'project': 'otherproj', 'kind': 'job', 'id': lease['id']}, worker)


def test_explicit_handoff_once_no_model_recovery_claim(collab):
    s, owner, worker, dot, room, agents, clock, scope = collab
    req, lease = claimed(collab, next_assignee_agent_id=agents[1]['id'])
    args = result_args(collab, lease)
    done = s.submit(args, worker)
    assert done['handoff_job_id']
    assert s.submit(args, worker) == done
    jobs = s.store.all('SELECT * FROM collaboration_jobs')
    assert len(jobs) == 2
    assert jobs[1]['next_assignee_agent_id'] == ''
    assert s.agent_view(room, s.agent(room, agents[0]['id']))['chat_identity_verified'] is False


def test_schema_constraints_and_text_redaction():
    from shared.collaboration_contracts import Rule, Result
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Result.model_validate({'outcome': 'healthy', 'summary': 'No evidence'})
    with pytest.raises(ValidationError):
        Rule.model_validate({'rule_id': 'r', 'probe_id': 'p', 'metric': 'availability',
            'open_when': {'operator': 'lt', 'value': float('nan')},
            'close_when': {'operator': 'gt', 'value': 0.9}, 'require_recovery_probe': 'p'})
    assert redact({'password': 'synthetic-sensitive-value'})['password'] == '[REDACTED]'
    assert 'synthetic-token' not in redact('Authorization: Bearer synthetic-token')


def test_public_job_evidence_keeps_original_budget_lease_and_fence(collab):
    from shared.public_collaboration import request, tool_definitions
    s, _, worker, _, room, _, clock, scope = collab
    _, leased = claimed(collab)
    identifier = 'bounded-evidence'
    body = {'observed': 'fixture'}
    s.store.execute('INSERT INTO monitor_evidence VALUES (?,?,?,?,?,?,?,?,?)',
        (identifier, room['id'], None, canonical(body), digest(body), clock[0], clock[0] + 3600, 1, 'aggregate_probe'))
    job = s.object('collaboration_jobs', room, leased['job_id'])
    context = json.loads(job['context'])
    context['evidence_refs'] = [identifier]
    s.store.execute('UPDATE collaboration_jobs SET context=? WHERE id=?', (canonical(context), job['id']))
    args = {**scope, 'kind': 'evidence', 'id': identifier, 'job_id': job['id'],
            'attempt': leased['attempt'], 'fencing_token': leased['fencing_token']}
    canonical_call = request('collaboration_read', args)
    assert canonical_call['tool'] == 'collaboration_work'
    definition = next(item for item in tool_definitions() if item['name'] == 'collaboration_work')
    assert not definition['annotations']['readOnlyHint'] and not definition['annotations']['idempotentHint']
    before = s.object('collaboration_jobs', room, job['id'])['tool_calls']
    legacy = s.invoke('collaboration_read', args, worker)
    assert s.object('collaboration_jobs', room, job['id'])['tool_calls'] == before + 1
    current = s.invoke(canonical_call['tool'], canonical_call['arguments'], worker)
    assert current == legacy
    assert s.object('collaboration_jobs', room, job['id'])['tool_calls'] == before + 2
    with pytest.raises(DevError) as denied:
        s.invoke('collaboration_query', canonical_call['arguments'], worker)
    assert denied.value.code == 'INVALID_ARGUMENTS'
    for name, raw in (('collaboration_read', args), (canonical_call['tool'], canonical_call['arguments'])):
        with pytest.raises(DevError) as denied:
            s.invoke(name, {**raw, 'fencing_token': raw['fencing_token'] + 1}, worker)
        assert denied.value.code == 'LEASE_EXPIRED'
    assert s.object('collaboration_jobs', room, job['id'])['tool_calls'] == before + 2
    maximum = context['budget']['max_tool_calls_per_job']
    s.store.execute('UPDATE collaboration_jobs SET tool_calls=? WHERE id=?', (maximum, job['id']))
    for name, raw in (('collaboration_read', args), (canonical_call['tool'], canonical_call['arguments'])):
        with pytest.raises(DevError) as denied:
            s.invoke(name, raw, worker)
        assert denied.value.code == 'BUDGET_EXCEEDED'
    assert s.object('collaboration_jobs', room, job['id'])['tool_calls'] == maximum
    clock[0] = leased['lease_until'] + 1
    for name, raw in (('collaboration_read', args), (canonical_call['tool'], canonical_call['arguments'])):
        with pytest.raises(DevError) as denied:
            s.invoke(name, raw, worker)
        assert denied.value.code == 'LEASE_EXPIRED'
