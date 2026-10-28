"""Actual archived baseline Hub over HTTP after an offline v2 admission freeze.

Only ephemeral data, fixture credentials and loopback child processes are used.
This proves the recorded baseline's guards, not arbitrary historical releases,
physical Agent process locks, a production rollback or host callback behavior.
Run this explicit historical gate with pytest tests/acceptance_multidot_old_binary.py
from a Git checkout containing BASELINE; the default source-ZIP CI does not collect it.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tarfile
from dataclasses import replace

import httpx
import pytest

from hub.collaboration.common import MESSAGE_EVENT, canonical, digest
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from scripts import prepare_multidot_rollback as freeze
from shared.crypto import digest as secret_digest, token
from shared.public_collaboration import request as public_request, resolve as resolve_public
from tests.collaboration_support import collab as collab, key
from tests.support import wait_for, wait_for_hub

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '3f004f54be61d34e93080e9edda07aeb1a1adb08'
TABLES = (
    'operations', 'coordination_operations', 'coordination_work',
    'coordination_attempts', 'coordination_approvals', 'collaboration_messages',
    'collaboration_dot_messages', 'dot_mailbox_bindings', 'dot_mailbox_entries',
    'dot_consumer_sessions', 'team_goals', 'team_message_bindings', 'mcp_event_deliveries',
)
FROZEN = {'JOIN_SLOT_REVOKED', 'DELEGATION_POLICY_INACTIVE',
          'DELEGATION_POLICY_CHANGED', 'GOAL_INACTIVE'}


def snapshot(database):
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        return {name: [dict(row) for row in db.execute('SELECT * FROM ' + name +
                (" WHERE subscription_id IN (SELECT id FROM mcp_event_subscriptions WHERE name='codepier.collaboration.inbox_dirty.v2')"
                 if name == 'mcp_event_deliveries' else '') + ' ORDER BY rowid')]
                for name in TABLES}


def readonly_marker(database):
    with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True) as db:
        return dict(db.execute("SELECT key,value FROM meta WHERE key IN ('schema','multidot_rollback_freeze')"))


def archived_source(directory):
    target = directory / 'baseline-source'
    target.mkdir()
    archive = directory / 'baseline.tar'
    subprocess.run(['git', 'archive', '--format=tar', '--output=' + str(archive), BASELINE],
                   cwd=ROOT, check=True, capture_output=True, timeout=30)
    with tarfile.open(archive) as stream:
        stream.extractall(target, filter='data')
    assert not (target / 'hub/collaboration/dot_consumers.py').exists()
    expected = subprocess.run(['git', 'show', BASELINE + ':hub/collaboration/service.py'],
                             cwd=ROOT, check=True, capture_output=True, timeout=10).stdout
    assert (target / 'hub/collaboration/service.py').read_bytes() == expected
    return target


@contextlib.contextmanager
def child_hub(source, data_dir, evidence_dir, label, legacy_event):
    """Launch exactly one owned Hub; errors never restart or replay a child."""
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    env = {
        **os.environ, 'PYTHONPATH': str(source), 'PYTHONUNBUFFERED': '1',
        'HUB_DATA_DIR': str(data_dir), 'HUB_PUBLIC_URL': url, 'MCP_PUBLIC_URL': url,
        'CODEPIER_COLLABORATION_ENABLED': 'true',
        'CODEPIER_MCP_EVENTS_ENABLED': 'true',
        'ROLLBACK_FIXTURE_TRACE': str(evidence_dir / (label + '-dispatcher.jsonl')),
        'ROLLBACK_FIXTURE_V1_EVENT': legacy_event,
        'CODEPIER_MONITOR_COLLECTOR_ENABLED': 'false',
        'CODEPIER_ANALYSIS_DISPATCH_ENABLED': 'false',
    }
    proof = """
import hashlib, json, os, pathlib
import hub.collaboration.service as loaded
from hub.collaboration import network
from hub.collaboration.events import EventService
p = pathlib.Path(loaded.__file__).resolve()
assert p.is_relative_to(pathlib.Path.cwd())
print(json.dumps({'loaded_source': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}), flush=True)
trace = pathlib.Path(os.environ['ROLLBACK_FIXTURE_TRACE'])
def record(value):
    with trace.open('a') as stream:
        stream.write(json.dumps(value) + '\\n')
async def never_contact_external(url, body, headers):
    if not body.startswith(b'sealed:v1:'):
        payload = json.loads(body)
        if (payload.get('eventId') == os.environ['ROLLBACK_FIXTURE_V1_EVENT']
                and payload.get('name') == 'codepier.collaboration.message_mentioned.v1'):
            record({'phase': 'legacy_callback', 'event_id': payload['eventId']})
            return network.Reply(204)
    record({'phase': 'forbidden_callback', 'bytes': len(body), 'sealed': body.startswith(b'sealed:v1:')})
    raise AssertionError('Frozen fixture must not reach any callback transport')
network.webhook = never_contact_external
original_tick = EventService.tick
async def observed_tick(self, *args, **kwargs):
    record({'phase': 'start'})
    try:
        result = await original_tick(self, *args, **kwargs)
    except BaseException as exc:
        record({'phase': 'error', 'type': type(exc).__name__, 'message': str(exc)[:160]})
        raise
    record({'phase': 'complete'})
    return result
EventService.tick = observed_tick
from hub.__main__ import main
main()
"""
    log_path = evidence_dir / (label + '.log')
    with log_path.open('w') as log:
        child = subprocess.Popen([sys.executable, '-c', proof, '--data-dir', str(data_dir),
                                  'run', '--host', '127.0.0.1', '--port', str(port)],
                                 cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_for_hub(child, url, timeout=30)
            proof_row = json.loads(log_path.read_text().splitlines()[0])
            assert proof_row['loaded_source'] == str(source / 'hub/collaboration/service.py')
            assert proof_row['sha256'] == hashlib.sha256(
                (source / 'hub/collaboration/service.py').read_bytes()).hexdigest()
            trace = evidence_dir / (label + '-dispatcher.jsonl')
            rows = wait_for(lambda: ([json.loads(line) for line in trace.read_text().splitlines()]
                            if trace.exists() and any(word in trace.read_text() for word in ('complete', 'error'))
                            else None), timeout=6)
            assert any(row['phase'] == 'complete' for row in rows), rows
            assert not any(row['phase'] in {'error', 'forbidden_callback'} for row in rows), rows
            with httpx.Client(base_url=url, timeout=8, trust_env=False) as client:
                yield client, child.pid, proof_row, trace
            rows = [json.loads(line) for line in trace.read_text().splitlines()]
            assert not any(row['phase'] in {'error', 'forbidden_callback'} for row in rows), rows
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            assert child.poll() is not None


def mcp(client, secret, tool, arguments):
    response = client.post('/mcp', headers={
        'Accept': 'application/json, text/event-stream',
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret,
        'MCP-Protocol-Version': '2025-11-25',
    }, json={'jsonrpc': '2.0', 'id': key(), 'method': 'tools/call',
             'params': {'name': tool, 'arguments': arguments}})
    assert response.status_code == 200, (response.status_code, response.text[:300])
    payload = response.json()
    assert 'result' in payload, payload
    return payload['result']


def rejected(client, secret, tool, arguments):
    result = mcp(client, secret, tool, arguments)
    assert result.get('isError') is True, (tool, result)
    data = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    error = data.get('error', data)
    assert error['code'] in FROZEN, (tool, error)
    return {'tool': tool, 'action': arguments.get('action'), 'code': error['code']}


def accepted(client, secret, tool, arguments):
    result = mcp(client, secret, tool, arguments)
    assert result.get('isError') is not True, (tool, result)
    return result['structuredContent']


async def prepare_fixture(fixture):
    service, owner, worker, dot_actor, room, _, clock, scope = fixture
    service.runtime.collaboration = service
    broad = replace(worker, grant_id='broad', actor='mcp:broad:old-binary-fixture')
    teammate = replace(worker, grant_id='second', actor='mcp:second:old-binary-fixture')

    def join(actor, label):
        created = service.dots.create({**scope, 'conversation_id': room['id'], 'label': label,
            'capabilities': ['read'], 'duplex': True, 'confirm_tasks': True,
            'idempotency_key': key()}, owner)['dot']
        joined = service.dots.join({'code': created['join_code'], 'idempotency_key': key()}, actor)['slot']
        return {**joined, 'join_code': created['join_code']}

    mailbox = join(broad, 'v2 admission freeze')
    unrelated = join(broad, 'unrelated v1 on the same grant remains usable')
    coordinator, member = join(dot_actor, 'team coordinator'), join(teammate, 'team member')

    def message(dot, text, **extra):
        return service.chatroom.create({**scope, 'room_id': room['id'],
            'conversation_id': room['id'], 'mentions': [{'slot_id': dot['id']}],
            'body_text': text, 'client_message_id': key(), 'idempotency_key': key(),
            **extra}, owner)['message']

    source = message(mailbox, 'Read the current approved file after offline recovery.')
    interim = service.dot_chat.message({**scope, 'dot_id': mailbox['id'],
        'reply_to_id': source['id'], 'body_text': 'Fixture interim receipt.', 'complete': False,
        'idempotency_key': key()}, broad)['message']
    service.config = replace(service.config, events_enabled=True)

    async def callback(url, body, headers):
        payload = json.loads(body)
        if payload.get('type') == 'verification':
            return Reply(200, canonical({'challenge': payload['challenge']}).encode())
        return Reply(503)  # Persist one genuine sealed retry body before the offline freeze.

    service.events = EventService(service, callback)
    configured = service.dot_mailbox.configure({**scope, 'dot_id': mailbox['id'],
        'confirm_consumer_protocol': True, 'idempotency_key': key()}, broad)
    await service.events.subscribe({**configured['subscription_request'], 'delivery': {
        'mode': 'webhook', 'url': 'https://old-binary-fixture.example.invalid/events',
        'secret': 'whsec_' + base64.b64encode(b'isolated-old-binary-signing-key!!!').decode()}}, broad)
    assert service.dot_mailbox.binding(mailbox['id'])['state'] == 'active'
    message(mailbox, 'A pending v2 notification must not reach the old dispatcher transport.')
    await service.events.tick()
    assert service.store.one("SELECT id FROM mcp_event_deliveries WHERE body LIKE 'sealed:v1:%'")
    legacy_route = await service.events.subscribe({
        **service.dot_chat.subscription(room, service.dots.find(unrelated['id'])),
        'delivery': {'mode': 'webhook', 'url': 'https://old-binary-fixture.example.invalid/legacy',
                     'secret': 'whsec_' + base64.b64encode(b'isolated-v1-route-signing-key!!!!!').decode()}}, broad)
    legacy_source = message(unrelated, 'This unrelated v1 route must still receive its original event.')
    legacy_event = service.store.one("""SELECT id FROM mcp_event_outbox WHERE name=?
        AND json_extract(data,'$.message_id')=? AND json_extract(data,'$.recipient_slot_id')=?""",
        (MESSAGE_EVENT, legacy_source['id'], unrelated['id']))
    assert legacy_event

    team = service.coordination.teams.create({**scope, 'conversation_id': room['id'],
        'label': 'old binary boundary', 'dot_ids': [coordinator['id'], member['id']],
        'coordinator_dot_id': coordinator['id'], 'project_ids': ['proj'],
        'capabilities': ['read'], 'confirm_tasks': True, 'idempotency_key': key()}, owner)['team']
    original = message(coordinator, 'Inspect the current fixture files as one team.', team_id=team['id'])
    goal = service.coordination.teams.from_message({**scope, 'team_id': team['id'],
        'message_id': original['id'], 'source_version': original['version'],
        'source_digest': digest(original['body_text']), 'idempotency_key': key()}, dot_actor)['goal']
    pending = message(coordinator, 'A separately selected source without a Goal.', team_id=team['id'])
    item = service.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (goal['id'],))
    item = service.coordination.work_claim({**scope, 'goal_id': goal['id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'coordinator_epoch': 1, 'expected_intent_revision': 1,
        'idempotency_key': key()}, dot_actor)['work_item']
    queued = service.coordination.work_create({**scope, 'goal_id': goal['id'],
        'coordinator_epoch': 1, 'expected_intent_revision': 1, 'assignee_dot_id': member['id'],
        'assignee_grant_id': 'second', 'target_project_id': 'proj',
        'objective': 'Read only after assignment', 'acceptance': 'Report the original source',
        'idempotency_key': key()}, dot_actor)['work_item']
    now = clock[0]
    for identifier, state, result in (
        ('original-unknown-operation', 'needs_review', None),
        ('original-settled-operation', 'succeeded', canonical({'ok': True, 'data': {'fixture': True}})),
    ):
        service.store.execute("""INSERT INTO operations
            (id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,created,updated,result)
            VALUES (?,'dev','proj','fixture','dot','read','{}','original-fixture',?,?,?,?)""",
            (identifier, state, now, now, result))
        service.store.execute("""INSERT INTO coordination_operations
            (operation_id,goal_id,work_item_id,approval_id,attempt,fencing_token,project_id,grant_id,request_digest,created)
            VALUES (?,?,?,?,?,?,'proj','dot','original-fixture',?)""",
            (identifier, goal['id'], item['id'], goal['approval_id'], item['attempt'], item['fencing_token'], now))
    secrets = {}
    for grant in ('worker', 'broad', 'dot', 'second'):
        secret = 'rd_' + token()
        secrets[grant] = secret
        service.store.execute("INSERT INTO tokens VALUES (?,?,?,'pat',?,?)",
                              (key(), secret_digest(secret), grant, now + 3600, now))
    policy = service.dots.policy(room, service.dots.find(mailbox['id']))
    return {
        'service': service, 'scope': scope, 'mailbox': mailbox, 'unrelated': unrelated,
        'coordinator': coordinator, 'member': member, 'source': source, 'interim': interim,
        'goal': goal, 'item': item, 'queued': queued, 'pending': pending, 'policy': policy,
        'secrets': secrets, 'checkpoint': service.dots.find(mailbox['id'])['checkpoint'],
        'legacy_route': legacy_route['id'], 'legacy_event': legacy_event['id'],
        'handling_scope': service.message_handling.scope_digest(
            room, service.dots.find(mailbox['id'])),
    }


def denial_cases(c):
    scope, slot, source, item, goal = c['scope'], c['mailbox'], c['source'], c['item'], c['goal']
    dot = {**scope, 'dot_id': slot['id']}
    work = {**scope, 'goal_id': goal['id']}
    lease = {**work, 'work_item_id': item['id'], 'attempt': item['attempt'],
             'fencing_token': item['fencing_token'], 'idempotency_key': key()}
    policy = {**scope, 'policy_id': c['policy']['id'], 'policy_version': c['policy']['version'],
              'mode': 'managed_execution', 'checkpoint': c['checkpoint']}
    pairs = [
        ('broad', 'collaboration_join', 'collaboration', 'join',
         {'code': slot['join_code'], 'idempotency_key': key()}),
        ('broad', 'collaboration_dot_message', 'collaboration', 'dot_message',
         {**dot, 'reply_to_id': source['id'], 'body_text': 'Must not publish after freeze.',
          'complete': True, 'idempotency_key': key()}),
        ('broad', 'collaboration_dot_message_v2', 'collaboration', 'dot_message_v2',
         {**dot, 'message_schema_version': 2, 'reply_to_id': source['id'],
          'body_text': 'A v2 publication must not bypass the same slot freeze.',
          'complete': True, 'idempotency_key': key()}),
        ('dot', 'collaboration_dot_task', 'collaboration_work', 'from_message',
         {**scope, 'dot_id': c['coordinator']['id'], 'message_id': c['pending']['id'],
          'expected_version': c['pending']['version'], 'confirm_task': True, 'idempotency_key': key()}),
        ('broad', 'collaboration_dot_ack', 'collaboration', 'dot_ack',
         {**dot, 'message_id': source['id'], 'disposition': 'handled', 'idempotency_key': key()}),
        ('broad', 'collaboration_dot_handle', 'collaboration', 'dot_handle',
         {**dot, 'handling_schema_version': 1, 'scope_digest': c['handling_scope'],
          'inputs': [{'message_id': source['id'], 'source_version': source['version'],
                      'source_digest': digest(source['body_text']), 'expected_revision': 0}],
          'handling_state': 'observed', 'reason_code': 'received', 'idempotency_key': key()}),
        ('broad', 'collaboration_dot_reply_update', 'collaboration', 'dot_reply_update',
         {**dot, 'message_id': c['interim']['id'], 'expected_version': c['interim']['version'],
          'body_text': 'Must not overwrite an old reply.', 'complete': False, 'idempotency_key': key()}),
        ('broad', 'collaboration_dot_task', 'collaboration_work', 'from_message',
         {**dot, 'message_id': source['id'], 'expected_version': source['version'],
          'confirm_task': True, 'idempotency_key': key()}),
        ('broad', 'collaboration_delegation_inbox', 'collaboration_query', 'inbox', policy),
        ('dot', 'collaboration_work_execute', 'collaboration_work', 'execute',
         {**lease, 'tool': 'read', 'arguments': {'path': 'README.md'}}),
        ('second', 'collaboration_work_claim', 'collaboration_work', 'claim',
         {**work, 'work_item_id': c['queued']['id'], 'expected_version': c['queued']['version'],
          'idempotency_key': key()}),
        ('dot', 'collaboration_work_create', 'collaboration_work', 'create',
         {**work, 'objective': 'Must not create another step', 'acceptance': 'No new work',
          'target_project_id': 'proj', 'assignee_grant_id': 'dot', 'idempotency_key': key()}),
        ('dot', 'collaboration_work_assign', 'collaboration_work', 'assign',
         {**work, 'work_item_id': c['queued']['id'], 'expected_version': c['queued']['version'],
          'assignee_grant_id': 'dot', 'idempotency_key': key()}),
    ]
    for grant, legacy, _public, _action, arguments in pairs:
        yield grant, legacy, arguments
        public = public_request(legacy, arguments)
        resolve_public(public['tool'], public['arguments'])
        yield grant, public['tool'], public['arguments']


@pytest.mark.slow
@pytest.mark.asyncio
async def test_archived_baseline_http_cannot_bypass_frozen_v2_admission(collab, tmp_path):
    c = await prepare_fixture(collab)
    service = c['service']
    data_dir = service.store.directory
    database = service.store.state_directory / 'hub.sqlite3'
    before = snapshot(database)
    baseline_source = archived_source(tmp_path)
    service.store.close()  # No current writer remains when reviewing/applying the plan.

    plan_path, manifest_path = tmp_path / 'reviewed-plan.json', tmp_path / 'freeze-receipt.json'
    base_command = [sys.executable, str(ROOT / 'scripts/prepare_multidot_rollback.py'),
                    '--database', str(database)]
    dry = subprocess.run([*base_command, '--plan-out', str(plan_path)], cwd=ROOT,
                         check=True, capture_output=True, text=True, timeout=20)
    review = json.loads(dry.stdout)
    assert review['state'] == 'dry_run'
    assert snapshot(database) == before
    assert 'multidot_rollback_freeze' not in readonly_marker(database)
    plan = json.loads(plan_path.read_text())
    assert review['plan_sha256'] == freeze.digest(plan)
    assert plan['source'] == freeze.source_stamp(ROOT)
    assert plan['guard_baseline'] == BASELINE
    frozen_ids = {row['id'] for row in plan['targets']['collaboration_join_slots']}
    assert frozen_ids == {c['mailbox']['id'], c['coordinator']['id'], c['member']['id']}
    assert c['pending']['id'] in {row['message_id'] for row in plan['scope']['selected_team_sources']}
    applied = subprocess.run([*base_command, '--apply', str(plan_path),
                              '--expected-plan-sha256', review['plan_sha256'],
                              '--confirm-hub-stopped', '--manifest-out', str(manifest_path)],
                             cwd=ROOT, check=True, capture_output=True, text=True, timeout=20)
    receipt = json.loads(applied.stdout)
    assert receipt == json.loads(manifest_path.read_text())
    assert receipt['state'] == 'admission_frozen' and receipt['operations_replayed'] == 0
    assert snapshot(database) == before
    assert readonly_marker(database)['multidot_rollback_freeze'] == review['plan_sha256']

    evidence = {'baseline': BASELINE, 'plan_sha256': review['plan_sha256'],
                'physical_agent_lock_verification': False, 'real_host_verification': False,
                'downgraded_key_maintenance_allowed': False, 'runs': []}
    for label, code in [('archived-baseline', baseline_source), ('current-forward', ROOT)]:
        with child_hub(code, data_dir, tmp_path, label, c['legacy_event']) as (client, pid, proof, trace):
            denials = [rejected(client, c['secrets'][grant], tool, args)
                       for grant, tool, args in denial_cases(c)]
            assert len(denials) == 26
            goal = accepted(client, c['secrets']['dot'], 'collaboration_query',
                            {**c['scope'], 'action': 'goal', 'goal_id': c['goal']['id']})
            assert goal['goal']['state'] == 'paused'
            # An unrelated v1 Dot still works; the freeze does not revoke an entire grant.
            connected = accepted(client, c['secrets']['broad'], 'collaboration_query',
                                 {**c['scope'], 'action': 'dot_connection',
                                  'dot_id': c['unrelated']['id']})
            assert connected['dot_id'] == c['unrelated']['id']
            for identifier, state in [('original-unknown-operation', 'needs_review'),
                                      ('original-settled-operation', 'succeeded')]:
                operation = mcp(client, c['secrets']['dot'], 'task_query',
                                {'operation': 'get', 'operation_ids': [identifier], 'project': 'proj'})
                # needs_review is deliberately surfaced as an error-status receipt,
                # not a failed read or permission to rerun the original side effect.
                rows = operation['structuredContent']['operations']
                assert len(rows) == 1 and rows[0]['id'] == identifier and rows[0]['state'] == state
                assert operation.get('isError', False) is (state == 'needs_review')
            assert snapshot(database) == before
            phases = [json.loads(line) for line in trace.read_text().splitlines()]
            callbacks = [row for row in phases if row['phase'] == 'legacy_callback']
            assert len(callbacks) == (1 if label == 'archived-baseline' else 0)
            with sqlite3.connect(database) as db:
                delivery = db.execute('SELECT state,attempt FROM mcp_event_deliveries WHERE subscription_id=? AND event_id=?',
                                      (c['legacy_route'], c['legacy_event'])).fetchone()
                assert delivery == ('accepted', 1)
            evidence['runs'].append({'kind': label, 'child_pid': pid, 'source': proof,
                                     'denials': denials, 'original_operations_readable': 2,
                                     'unrelated_same_grant_v1_readable': True,
                                     'legacy_callbacks': len(callbacks), 'dispatcher_errors': 0,
                                     'callback_transport': 'isolated_in_process_fixture',
                                     'sealed_retry_ledger_unchanged': True, 'ledger_unchanged': True})
        assert snapshot(database) == before
    assert len({run['child_pid'] for run in evidence['runs']}) == 2
    (tmp_path / 'acceptance.json').write_text(json.dumps(evidence, indent=2) + '\n')
