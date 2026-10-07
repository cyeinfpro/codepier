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


def key():
    return uuid.uuid4().hex


@pytest.fixture
def collab(tmp_path):
    store = Store(tmp_path / 'hub')
    with store.transaction():
        migrate(store.db)
    seed_owner(store, 'owner', 'admin')
    seed_owner(store, 'other', 'other')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,?)", (store.encrypt('synthetic-test-device'), time.time()))
    for pid in ('proj', 'otherproj'):
        store.execute('''INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created)
            VALUES (?,?,?,'dev','/tmp/fixture','write',1,?)''', (pid, pid, pid, time.time()))
    for grant in ('worker', 'dot', 'second'):
        seed_grant(store, grant, 'owner', scopes=('read',), projects=('proj',))
    seed_grant(store, 'broad', 'owner', scopes=('read', 'write', 'execute'), projects=('proj',))
    runtime = Runtime(store)
    clock = [time.time()]
    service = CollaborationService(runtime, CollaborationConfig(enabled=True), clock=lambda: clock[0])
    owner = Principal('panel:admin', 'owner', {'read', 'write'}, ['*'], admin=True)
    worker = Principal('mcp:worker:fixture', 'owner', {'read'}, ['proj'], grant_id='worker')
    dot = replace(worker, grant_id='dot', actor='mcp:dot:fixture')
    scope = {'project': 'proj', 'environment_id': 'production'}
    room = service.room_create({**scope, 'idempotency_key': key()}, owner)['room']
    agents = [service.register_agent({**scope, 'label': 'same name', 'kind': kind,
              'grant_id': grant, 'idempotency_key': key()}, owner)
              for kind, grant in [('work_cloud', 'worker'), ('dot', 'dot')]]
    yield service, owner, worker, dot, room, agents, clock, scope
    store.close()


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
