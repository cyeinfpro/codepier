"""Real Hub/Agent process faults with isolated HTTP native-event fixtures.

The Hub and Agent are actual child processes with persistent fixture SQLite and
journals. Only the native callback transport is mapped to a localhost receiver;
this does not prove a real Dot host, external network, or physical device.
All expiry waits use wall clock. Only the explicit unknown-operation fault below
edits a fixture row while its Hub is stopped; production databases are never read.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest

from tests.collaboration_support import key
from tests.support import BASE, Stack, wait_for, wait_for_bound_port, wait_for_hub

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SECRET = 'whsec_' + base64.b64encode(b'process-recovery-fixture-secret!!').decode()
PROOF_FIELDS = ('consumer_session_id', 'consumer_fence', 'consumer_proof')


class Callback:
    def __init__(self):
        self.records = []
        self.lock = threading.Lock()
        self.block_slot = None
        self.blocked = threading.Event()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                payload = json.loads(raw)
                if payload.get('type') == 'verification':
                    body = json.dumps({'challenge': payload['challenge']}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                record = {'body': raw, 'payload': payload, 'headers': dict(self.headers),
                          'observed_at': time.time(), 'path': self.path}
                with outer.lock:
                    outer.records.append(record)
                if (payload.get('data', {}).get('recipient_slot_id') == outer.block_slot
                        and not outer.blocked.is_set()):
                    outer.blocked.set()
                    outer.release.wait(45)
                    self.close_connection = True  # Accepted body, deliberately no HTTP response.
                    return
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self.end_headers()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def events(self, dot_id):
        with self.lock:
            return [item for item in self.records
                    if item['payload'].get('data', {}).get('recipient_slot_id') == dot_id
                    and item['payload']['data'].get('wake_ticket')]

    def room_events(self, member_id):
        with self.lock:
            return [item for item in self.records
                    if item['payload'].get('data', {}).get('member_id') == member_id
                    and item['payload']['data'].get('wake_ticket')]

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class ProcessStack(Stack):
    def __init__(self, directory, callback):
        self.callback_port = callback.port
        self.hub_pids = []
        super().__init__(directory)

    def start_hub(self):
        receipt = self.directory / ('hub-port-' + uuid.uuid4().hex + '.json')
        self.hub = subprocess.Popen(
            [os.environ.get('CODEPIER_TEST_STACK_PYTHON') or sys.executable,
             '-m', 'tests.test_multidot_process_recovery',
             '--hub', str(self.hubdir), str(self.port), str(self.callback_port), str(receipt)],
            cwd=BASE, env=self.env, stdout=self.hub_log, stderr=subprocess.STDOUT)
        self.hub_pids.append(self.hub.pid)
        try:
            self.port = wait_for_bound_port(self.hub, receipt)
            self.url = f'http://127.0.0.1:{self.port}'
            for key in ('HUB_PUBLIC_URL', 'MCP_PUBLIC_URL'):
                if self.env.get(key) == 'http://127.0.0.1:0':
                    self.env[key] = self.url
            wait_for_hub(self.hub, self.url)
        except AssertionError as exc:
            from shared.audit_redaction import redact_text
            details = redact_text((self.directory / 'hub.log').read_text(
                encoding='utf-8', errors='replace'))[-6000:]
            raise AssertionError(f'{exc}\n{details}') from exc

    def crash_hub(self):
        assert self.hub.poll() is None
        self.hub.kill()  # Only the exact child this fixture created.
        self.hub.wait(timeout=10)

    def restart_hub(self):
        assert self.hub.poll() is not None
        self.start_hub()
        self.login()


@contextlib.contextmanager
def stack(directory):
    receiver = Callback()
    s = ProcessStack.__new__(ProcessStack)
    try:
        s.__init__(directory, receiver)
        yield s, receiver
    finally:
        # Unblock this fixture's controlled command even when an assertion fails.
        # The marker belongs to this newly created fixture, never a user project.
        if getattr(s, 'directory', None) is not None and s.directory.is_dir():
            (s.directory / 'release-first-command').touch(exist_ok=True)
        s.close()
        receiver.close()


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'true')
    monkeypatch.setenv('CODEPIER_MCP_EVENTS_ENABLED', 'true')
    monkeypatch.setenv('CODEPIER_MONITOR_COLLECTOR_ENABLED', 'false')
    monkeypatch.setenv('CODEPIER_ANALYSIS_DISPATCH_ENABLED', 'false')


def database(s):
    matches = list(s.hubdir.glob('**/hub.sqlite3'))
    assert len(matches) == 1
    return matches[0]


def rows(s, sql, arguments=()):
    with sqlite3.connect(database(s).as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, arguments)]


def mcp(s, name, arguments, *, ok=True):
    result = s.mcp(name, arguments)
    assert bool(result.get('isError')) is not ok, result
    return result.get('structuredContent', result)


def native(s, method, arguments):
    response = s.rpc(method, {**arguments, '_meta': {
        'io.modelcontextprotocol/protocolVersion': '2026-07-28',
        'io.modelcontextprotocol/clientCapabilities': {}}},
        headers={'MCP-Protocol-Version': '2026-07-28', 'Mcp-Method': method})
    assert response.is_success, response.text
    value = response.json()
    assert 'error' not in value, value
    return value['result']


def setup(s, *, execute=False):
    scope = {'project': s.project['id'], 'environment_id': 'production'}
    room = s.must(s.client.post('/api/collaboration/room',
                               json={**scope, 'idempotency_key': key()}))['room']
    dots = []
    for number in range(2):
        args = {**scope, 'conversation_id': room['id'], 'label': 'Process fixture ' + str(number),
                'capabilities': ['read', 'write', 'execute'] if execute else ['read'],
                'duplex': True, 'confirm_tasks': True, 'idempotency_key': key()}
        if execute:
            args['acknowledge_unsandboxed_exec'] = True
        created = s.must(s.client.post('/api/collaboration/dot', json=args))['dot']
        joined = mcp(s, 'collaboration_join',
                     {'code': created['join_code'], 'idempotency_key': key()})
        config = mcp(s, 'collaboration_dot_mailbox_configure', {
            **scope, 'dot_id': created['id'], 'protocol_version': 2,
            'confirm_consumer_protocol': True, 'idempotency_key': key()})
        subscription = native(s, 'events/subscribe', {**config['subscription_request'],
            'delivery': {'mode': 'webhook',
                         'url': 'https://process-fixture.example.invalid/' + created['id'],
                         'secret': SECRET}})
        dots.append({'id': created['id'], 'binding_generation': config['binding_generation'],
                     'subscription_id': subscription['id'], 'joined': joined})
    return scope, room, dots


def message(s, scope, room, dot, text, **extra):
    return s.must(s.client.post('/api/collaboration/message', json={
        **scope, 'room_id': room['id'], 'conversation_id': room['id'], 'body_text': text,
        'mentions': [{'slot_id': dot['id']}], 'client_message_id': key(),
        'idempotency_key': key(), **extra}))['message']


def page(s, scope, dot):
    return mcp(s, 'collaboration_dot_mailbox', {
        **scope, 'dot_id': dot['id'], 'binding_generation': dot['binding_generation']})


def begin_request(scope, dot, event, seconds=30):
    return {**scope, 'dot_id': dot['id'], 'binding_generation': dot['binding_generation'],
            'wake_ticket': event['payload']['data']['wake_ticket'],
            'consumer_run_id': key(), 'idempotency_key': key(), 'lease_seconds': seconds}


def proof(s, scope, dot, session):
    return {'dot_id': dot['id'], **{k: session[k] for k in PROOF_FIELDS},
            'expected_inbox_revision': page(s, scope, dot)['dirty_revision']}


@contextlib.contextmanager
def lost_reply_proxy(s):
    """Forward one real MCP call, retain its response privately, drop caller reply."""
    captured = {}
    complete = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in {'host', 'connection', 'content-length'}}
            with httpx.Client(timeout=20, trust_env=False) as client:
                response = client.post(s.url + '/mcp', content=raw, headers=headers)
            captured['response'] = response.json()
            complete.set()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, captured, complete
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_callback_and_begin_reply_loss_survive_real_hub_crashes_and_fence(tmp_path):
    with stack(tmp_path / 'lost-http') as (s, receiver):
        scope, room, dots = setup(s)
        a, b = dots
        receiver.block_slot = a['id']
        first = message(s, scope, room, a, 'Persist the first input across a lost callback response.')
        assert receiver.blocked.wait(15), 'No actual HTTP callback body was accepted'
        accepted = receiver.events(a['id'])[0]
        original_delivery = rows(s, 'SELECT * FROM mcp_event_deliveries WHERE event_id=? AND subscription_id=?',
                                 (accepted['payload']['eventId'], a['subscription_id']))
        assert len(original_delivery) == 1
        original_delivery = original_delivery[0]
        assert original_delivery['state'] == 'leased' and original_delivery['attempt'] == 1
        s.crash_hub()
        receiver.release.set()
        s.restart_hub()
        durable = rows(s, 'SELECT * FROM mcp_event_deliveries WHERE id=?',
                       (original_delivery['id'],))[0]
        assert durable['body'] == original_delivery['body']
        assert durable['attempt'] == 1  # Recovery waits for the original 30 second delivery lease.

        request = begin_request(scope, a, accepted)
        with lost_reply_proxy(s) as (port, captured, complete):
            with httpx.Client(timeout=20, trust_env=False) as client:
                with pytest.raises(httpx.RemoteProtocolError):
                    client.post(f'http://127.0.0.1:{port}/mcp', headers={
                        'Authorization': 'Bearer ' + s.pat,
                        'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                        'MCP-Protocol-Version': '2025-11-25'},
                        json={'jsonrpc': '2.0', 'id': 71, 'method': 'tools/call',
                              'params': {'name': 'collaboration_consumer_begin', 'arguments': request}})
            assert complete.wait(3)
            committed = captured['response']['result']
            assert not committed.get('isError'), committed
            committed = committed['structuredContent']
        s.crash_hub()
        s.restart_hub()
        replay = mcp(s, 'collaboration_consumer_begin', request)
        assert replay['replayed']
        assert {k: replay[k] for k in PROOF_FIELDS} == {k: committed[k] for k in PROOF_FIELDS}
        assert replay['lease_until'] == committed['lease_until']
        assert rows(s, 'SELECT COUNT(*) AS n FROM dot_consumer_sessions')[0]['n'] == 1
        denied = mcp(s, 'collaboration_consumer_begin',
                     {**request, 'dot_id': b['id'], 'binding_generation': b['binding_generation'],
                      'consumer_run_id': key(), 'idempotency_key': key()}, ok=False)
        assert 'DOT_WAKE_TICKET_INVALID' in json.dumps(denied)
        second = message(s, scope, room, a, 'A second input arrives while the old consumer owns the lease.')
        # Genuine downtime / lease expiry: do not edit timestamps or patch the service clock.
        remaining = max(replay['lease_until'], original_delivery['lease_until']) - time.time() + .2
        if remaining > 0:
            time.sleep(remaining)
        retry = wait_for(lambda: next((record for record in receiver.events(a['id'])[1:]
                         if record['headers']['webhook-id'] == accepted['headers']['webhook-id']), None), 12)
        assert retry['body'] == accepted['body']
        assert retry['headers']['X-MCP-Subscription-Id'] == accepted['headers']['X-MCP-Subscription-Id']
        next_event = wait_for(lambda: next((record for record in receiver.events(a['id'])
            if record['payload']['data']['wake_ticket'] != request['wake_ticket']), None), 12)
        fresh = mcp(s, 'collaboration_consumer_begin', begin_request(scope, a, next_event))
        assert fresh['consumer_fence'] > replay['consumer_fence']
        invalid = mcp(s, 'collaboration_dot_message', {
            **scope, **proof(s, scope, a, replay), 'reply_to_id': first['id'],
            'body_text': 'A stale writer must never publish.', 'complete': True,
            'idempotency_key': key()}, ok=False)
        assert 'DOT_CONSUMER_LEASE_EXPIRED' in json.dumps(invalid)
        for source in (first, second):
            result = mcp(s, 'collaboration_dot_message', {
                **scope, **proof(s, scope, a, fresh), 'reply_to_id': source['id'],
                'body_text': 'Recovered reply to ' + source['id'], 'complete': True,
                'idempotency_key': key()})
            assert result['complete']
        for item in page(s, scope, a)['items']:
            if item.get('ack_request'):
                action = item['ack_request']
                mcp(s, action['tool'], {**action['arguments'], **{k: fresh[k] for k in PROOF_FIELDS}})
        ended = mcp(s, 'collaboration_consumer_yield', {
            **scope, **proof(s, scope, a, fresh), 'idempotency_key': key()})
        assert ended['state'] == 'yielded' and not ended['more']
        assert not receiver.events(b['id'])
        assert rows(s, 'SELECT COUNT(*) AS n FROM mcp_event_subscriptions')[0]['n'] == 2
        assert rows(s, 'SELECT COUNT(*) AS n FROM coordination_operations')[0]['n'] == 0
        assert not rows(s, "SELECT id FROM collaboration_messages WHERE body LIKE '%stale writer%'")
        assert len(set(s.hub_pids)) == 3
        print(json.dumps({'scenario': 'callback_and_begin_reply_loss', 'hub_processes': len(s.hub_pids),
                          'delivery_bytes_preserved': True, 'old_fence_rejected': True,
                          'source_inputs': 2, 'native_subscriptions': 2, 'real_dot_host': False}))


def activate_consumers(s, receiver, scope, room, dots):
    sessions = []
    for dot in dots:
        source = message(s, scope, room, dot, 'Initialize the isolated process-recovery consumer.')
        event = wait_for(lambda: receiver.events(dot['id'])[-1] if receiver.events(dot['id']) else None, 12)
        session = mcp(s, 'collaboration_consumer_begin', begin_request(scope, dot, event, seconds=300))
        mcp(s, 'collaboration_dot_message', {
            **scope, **proof(s, scope, dot, session), 'reply_to_id': source['id'],
            'body_text': 'Synthetic consumer initialized.', 'complete': True, 'idempotency_key': key()})
        sessions.append(session)
    return sessions


def test_team_operation_and_agent_resource_ownership_survive_hub_process_restart(tmp_path):
    with stack(tmp_path / 'running-agent') as (s, receiver):
        s.stop_agent()
        s.config['shell'] = {'enabled': True, 'projects': ['ProjectAlpha'],
                             'command': ['/bin/sh', '-c'], 'inherit_env': False,
                             'max_timeout_seconds': 120}
        s.config_path.write_text(json.dumps(s.config))
        s.start_agent()
        scope, room, dots = setup(s, execute=True)
        sessions = activate_consumers(s, receiver, scope, room, dots)
        team = s.must(s.client.post('/api/collaboration/team-create', json={
            **scope, 'conversation_id': room['id'], 'label': 'Process recovery team',
            'dot_ids': [d['id'] for d in dots], 'coordinator_dot_id': dots[0]['id'],
            'project_ids': [s.project['id']], 'capabilities': ['read', 'write', 'execute'],
            'confirm_tasks': True, 'idempotency_key': key()}))['team']
        source = message(s, scope, room, dots[0], 'Run two isolated commands with one shared resource.',
                         team_id=team['id'])
        goal = mcp(s, 'collaboration_team_from_message', {
            **scope, **proof(s, scope, dots[0], sessions[0]), 'team_id': team['id'],
            'message_id': source['id'], 'source_version': source['version'],
            'source_digest': hashlib.sha256(
                'Run two isolated commands with one shared resource.'.encode()).hexdigest(),
            'idempotency_key': key()})['goal']
        context = {**scope, 'goal_id': goal['id'], 'coordinator_epoch': 1, 'expected_intent_revision': 1}
        works = []
        for index in (1, 0):
            work = mcp(s, 'collaboration_work_create', {
                **context, **proof(s, scope, dots[0], sessions[0]),
                'objective': 'Controlled real child command for Dot ' + str(index),
                'acceptance': 'One durable side effect and original receipt.',
                'target_project_id': s.project['id'], 'assignee_grant_id': s.grant,
                'assignee_dot_id': dots[index]['id'], 'required_capabilities': ['execute'],
                'idempotency_key': key()})['work_item']
            works.append(mcp(s, 'collaboration_work_claim', {
                **context, **proof(s, scope, dots[index], sessions[index]),
                'work_item_id': work['id'], 'expected_version': work['version'],
                'idempotency_key': key()})['work_item'])
        release = s.directory / 'release-first-command'
        code = ("from pathlib import Path; import time; "
                "p=Path('first-effects'); f=p.open('a'); f.write('1'); f.flush(); f.close(); "
                "Path('first-started').write_text('started'); "
                "deadline=time.monotonic()+60; "
                f"release=Path({str(release)!r}); "
                "exec('while not release.exists() and time.monotonic()<deadline:\\n time.sleep(.05)'); "
                "assert release.exists(); Path('first-finished').write_text('done')")
        first_args = {**context, **proof(s, scope, dots[1], sessions[1]),
            'work_item_id': works[0]['id'], 'attempt': works[0]['attempt'],
            'fencing_token': works[0]['fencing_token'], 'tool': 'exec',
            'arguments': {'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(code),
                          'resources': [{'kind': 'path', 'mode': 'write', 'name': '.'},
                                        {'kind': 'path', 'mode': 'read', 'name': str(release)}],
                          'timeout_seconds': 75, 'yield_seconds': 0},
            'idempotency_key': key()}
        first = mcp(s, 'collaboration_work_execute', first_args)
        wait_for(lambda: (s.projectalpha / 'first-started').exists(), 15)
        agent_pid = s.agent.pid
        s.crash_hub()
        # Explicit isolated uncertainty injection: only after the Hub child is stopped.
        with sqlite3.connect(database(s)) as db:
            db.execute("UPDATE operations SET state='unknown',transport_error=? WHERE id=?",
                       ('fixture: Hub lost the running operation receipt', first['operation_id']))
        assert rows(s, 'SELECT state FROM operations WHERE id=?', (first['operation_id'],))[0]['state'] == 'unknown'
        s.restart_hub()
        assert s.agent.pid == agent_pid and s.agent.poll() is None
        assert not release.exists() and not (s.projectalpha / 'first-finished').exists()
        assert (s.projectalpha / 'first-effects').read_text() == '1'
        recovered = mcp(s, 'collaboration_dot_recovery', {**scope, 'dot_id': dots[1]['id']})
        item = next(item for item in recovered['items'] if item['work_item_id'] == works[0]['id'])
        assert item['attempt'] == works[0]['attempt'] and item['fencing_token'] == works[0]['fencing_token']
        evidence = mcp(s, item['operations_request']['tool'], item['operations_request']['arguments'])
        assert [op['operation_id'] for op in evidence['items']] == [first['operation_id']]
        replay = mcp(s, 'collaboration_work_execute', first_args)
        assert replay['operation_id'] == first['operation_id']
        assert rows(s, 'SELECT COUNT(*) AS n FROM coordination_operations WHERE work_item_id=?',
                    (works[0]['id'],))[0]['n'] == 1
        other = mcp(s, 'collaboration_dot_recovery', {**scope, 'dot_id': dots[0]['id']})
        assert all(item['work_item_id'] != works[0]['id'] for item in other['items'])
        second_code = ("from pathlib import Path; Path('second-started').write_text('started'); "
                       "f=Path('second-effects').open('a'); f.write('1'); f.close()")
        second = mcp(s, 'collaboration_work_execute', {
            **context, **proof(s, scope, dots[0], sessions[0]), 'work_item_id': works[1]['id'],
            'attempt': works[1]['attempt'], 'fencing_token': works[1]['fencing_token'],
            'tool': 'exec', 'arguments': {
                'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(second_code),
                'resources': [{'kind': 'path', 'mode': 'write', 'name': '.'}],
                'timeout_seconds': 60, 'yield_seconds': 0}, 'idempotency_key': key()})
        blocking = wait_for(lambda: rows(s, "SELECT detail FROM operation_events WHERE operation_id=? AND stage='waiting_resource'",
                                         (second['operation_id'],)), 15)
        assert first['operation_id'] in json.dumps(blocking)
        assert not (s.projectalpha / 'second-started').exists()
        assert (s.projectalpha / 'first-effects').read_text() == '1'
        release.write_text('continue')
        assert s.poll(first['operation_id'], timeout=20)['state'] == 'succeeded'
        assert s.poll(second['operation_id'], timeout=20)['state'] == 'succeeded'
        assert (s.projectalpha / 'first-effects').read_text() == '1'
        assert (s.projectalpha / 'second-effects').read_text() == '1'
        target = page(s, scope, dots[1])['items']
        wrong = page(s, scope, dots[0])['items']
        assert any(item['reference'].get('operation_id') == first['operation_id'] for item in target)
        assert not any(item['reference'].get('operation_id') == first['operation_id'] for item in wrong)
        assert rows(s, 'SELECT COUNT(*) AS n FROM mcp_event_subscriptions')[0]['n'] == 2
        assert rows(s, 'SELECT COUNT(*) AS n FROM coordination_operations')[0]['n'] == 2
        assert len(set(s.hub_pids)) == 2
        print(json.dumps({'scenario': 'team_operation_with_agent_resource_claim',
                          'hub_processes': 2, 'agent_process_survived': True,
                          'original_operation_reused': True, 'real_side_effect_counts': [1, 1],
                          'resource_wait_observed': True, 'same_grant_targeting': True,
                          'real_dot_host': False}))


def native_room(s, receiver):
    """Use only owner HTTP and the actual public invitation/subscription flow."""
    room = s.must(s.client.post('/api/collaboration/rooms', json={
        'title': 'Native room process recovery',
        'projects': [{'project': s.project['id'], 'environment_id': 'production'}],
        'idempotency_key': key()}))['room']
    created = s.must(s.client.post('/api/collaboration/rooms/' + room['id'] + '/members', json={
        'name': 'Synthetic native host', 'project_scopes': [{
            'project_id': s.project['id'], 'capabilities': ['read', 'write', 'execute'],
            'execution_targets': ['project_agent']}], 'idempotency_key': key()}))
    flow = s.must(s.client.get('/api/collaboration/connect-flows/' + created['flow']['id']))
    instructions = flow['join_instructions']
    request, _ = json.JSONDecoder().raw_decode(instructions[instructions.index('{'):])
    assert request['action'] == 'join'
    assert request['code'].startswith('CPR-')
    joined = mcp(s, 'collaboration', request)
    identity = {field: joined[field] for field in
                ('conversation_id', 'member_id', 'binding_generation', 'connection_proof')}
    subscribed = native(s, 'events/subscribe', {**joined['subscription_request'],
        'delivery': {'mode': 'webhook',
            'url': 'https://process-fixture.example.invalid/native/' + identity['member_id'],
            'secret': SECRET}})
    event = wait_for(lambda: next(iter(receiver.room_events(identity['member_id'])), None), 15)
    assert event['headers']['X-MCP-Subscription-Id'] == subscribed['id']
    probe = mcp(s, 'collaboration', {'action': 'room_probe_read', **identity,
        'wake_ticket': event['payload']['data']['wake_ticket'], 'idempotency_key': key()})
    mcp(s, 'collaboration', {'action': 'room_probe_reply', **identity,
        'probe_nonce': probe['probe_nonce'], 'idempotency_key': key()})
    ready = s.must(s.client.get('/api/collaboration/rooms/' + room['id']))['room']['members']
    assert next(member for member in ready if member['id'] == identity['member_id'])['state'] == 'ready_work'
    session = mcp(s, 'collaboration', {'action': 'room_consumer_begin', **identity,
        'wake_ticket': event['payload']['data']['wake_ticket'], 'consumer_run_id': key(),
        'lease_seconds': 300, 'idempotency_key': key()})
    return room, identity, session, subscribed['id']


def native_inbox(s, identity):
    return mcp(s, 'collaboration_query', {'action': 'room_inbox', **identity, 'limit': 100})


def native_proof(s, identity, session):
    return {**{field: session[field] for field in PROOF_FIELDS},
            'expected_inbox_revision': native_inbox(s, identity)['inbox_revision']}


def native_source(s, identity, message_id):
    page = native_inbox(s, identity)
    entry = next(item for item in page['items']
                 if item['kind'] == 'message' and item['reference']['message_id'] == message_id)
    return {'source_message_id': message_id, 'source_version': entry['source_version'],
            'source_digest': entry['source_digest']}


def native_goal(s, identity, session, goal_id):
    return mcp(s, 'collaboration_query', {'action': 'goal', 'project': s.project['id'],
        'environment_id': 'production', 'goal_id': goal_id,
        **{field: session[field] for field in PROOF_FIELDS}})


def native_work_lease(s, identity, session, goal_id, item):
    context = native_goal(s, identity, session, goal_id)['room_work_context']
    return {'project': s.project['id'], 'environment_id': 'production', 'goal_id': goal_id,
        'work_item_id': item['id'], 'attempt': item['attempt'],
        'fencing_token': item['fencing_token'], **native_proof(s, identity, session),
        'coordinator_epoch': context['coordinator_epoch'],
        'expected_intent_revision': context['intent_revision']}


def test_native_room_real_agent_restart_stop_continue_and_unique_publication(tmp_path):
    """Synthetic native host transport; real Hub/Agent, HTTP, exec, files and crash."""
    with stack(tmp_path / 'native-room-agent') as (s, receiver):
        s.stop_agent()
        s.config['shell'] = {'enabled': True, 'projects': ['ProjectAlpha'],
                            'command': ['/bin/sh', '-c'], 'inherit_env': False,
                            'max_timeout_seconds': 120}
        s.config_path.write_text(json.dumps(s.config))
        s.start_agent()
        room, identity, session, subscription_id = native_room(s, receiver)
        messages_path = '/api/collaboration/rooms/' + room['id'] + '/messages'
        source_text = ('Write one durable start marker, wait for explicit stop, then preserve the '
                       'original receipt before continuing the same goal with a verified file and command.')
        source = s.must(s.client.post(messages_path, json={'body_text': source_text,
            'client_message_id': key(), 'idempotency_key': key()}))['message']
        start = mcp(s, 'collaboration', {'action': 'room_work_start', **identity,
            **native_source(s, identity, source['id']), **native_proof(s, identity, session),
            'target_project_id': s.project['id'], 'capabilities': ['read', 'write', 'execute'],
            'execution_targets': ['project_agent'], 'acceptance': 'Original receipts and exactly one effect per command.',
            'confirm_task': True, 'idempotency_key': key()})
        goal_id = start['goal_id']
        assert start['scheduled'] and start['next_request']
        first_item = mcp(s, start['next_request']['tool'], start['next_request']['arguments'])['work_item']
        release = s.directory / 'release-first-command'
        first_code = ("from pathlib import Path; import time; "
            "p=Path('native-first-effects'); f=p.open('a'); f.write('1'); f.flush(); f.close(); "
            "Path('native-first-started').write_text('started'); "
            f"release=Path({str(release)!r}); deadline=time.monotonic()+90; "
            "exec('while not release.exists() and time.monotonic()<deadline:\\n time.sleep(.05)'); "
            "assert release.exists(); Path('native-first-finished').write_text('done')")
        first_args = {**native_work_lease(s, identity, session, goal_id, first_item),
            'tool': 'exec', 'arguments': {
                'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(first_code),
                'resources': [{'kind': 'path', 'mode': 'write', 'name': '.'},
                              {'kind': 'path', 'mode': 'read', 'name': str(release)}],
                'timeout_seconds': 100, 'yield_seconds': 0}, 'idempotency_key': key()}
        first = mcp(s, 'collaboration_work_execute', first_args)
        wait_for(lambda: (s.projectalpha / 'native-first-started').exists(), 15)
        assert (s.projectalpha / 'native-first-effects').read_text() == '1'
        started = time.perf_counter()
        chat = s.must(s.client.post(messages_path, json={
            'body_text': '运行中仍可交谈；这条消息不增加执行权限。',
            'client_message_id': key(), 'idempotency_key': key()}))['message']
        message_seconds = time.perf_counter() - started
        assert message_seconds < 5, 'One local message request stalled behind the Agent command'
        assert chat['id'] != source['id'] and not (s.projectalpha / 'native-first-finished').exists()

        agent_pid = s.agent.pid
        s.crash_hub()
        s.restart_hub()
        assert s.agent.pid == agent_pid and s.agent.poll() is None
        assert (s.projectalpha / 'native-first-effects').read_text() == '1'
        assert not release.exists() and not (s.projectalpha / 'native-first-finished').exists()
        recovered = native_goal(s, identity, session, goal_id)
        assert recovered['goal']['id'] == goal_id
        assert [operation['operation_id'] for operation in recovered['operations']] == [first['operation_id']]
        recovered_item = next(item for item in recovered['work_items'] if item['id'] == first_item['id'])
        assert (recovered_item['attempt'], recovered_item['fencing_token']) == (
            first_item['attempt'], first_item['fencing_token'])
        original = mcp(s, 'task_query', {'operation': 'get',
            'operation_ids': [first['operation_id']]})['operations'][0]
        assert original['operation_id'] == first['operation_id']
        assert original['state'] in {'running', 'unknown', 'reconnecting'}
        replay = mcp(s, 'collaboration_work_execute', {
            **first_args, **native_proof(s, identity, session)})
        assert replay['operation_id'] == first['operation_id']
        assert (s.projectalpha / 'native-first-effects').read_text() == '1'

        started = time.perf_counter()
        paused = s.must(s.client.post('/api/collaboration/work/' + goal_id + '/control', json={
            'action': 'pause', 'expected_version': recovered['goal']['version'], 'idempotency_key': key()}))
        stop_seconds = time.perf_counter() - started
        assert stop_seconds < 5, 'One local stop request stalled behind the Agent command'
        assert paused['work']['id'] == goal_id
        assert [operation['operation_id'] for operation in paused['operations']] == [first['operation_id']]
        # Pausing the goal itself requests cancellation. The original operation
        # may already have reached its terminal state before this explicit check.
        cancel_result = s.mcp('process', {'operation': 'cancel',
            'operation_ids': [first['operation_id']]})
        cancellation = cancel_result['structuredContent']['operations'][0]
        assert cancellation['operation_id'] == first['operation_id']
        if cancel_result.get('isError'):
            assert cancellation['state'] == 'cancelled' and cancellation['cancel_requested'] is False, cancel_result
        else:
            assert cancellation['cancel_requested'] is True or cancellation['state'] == 'cancelled', cancel_result
        terminal = s.poll(first['operation_id'], timeout=20)
        assert terminal['state'] == 'cancelled', terminal
        assert (s.projectalpha / 'native-first-effects').read_text() == '1'
        assert not (s.projectalpha / 'native-first-finished').exists()

        mcp(s, 'collaboration', {'action': 'room_reply', **identity,
            **native_source(s, identity, chat['id']), **native_proof(s, identity, session),
            'body_text': '原命令已停止；已保留同一个操作回执。', 'complete': True,
            'idempotency_key': key()})
        latest = native_goal(s, identity, session, goal_id)
        resume_key = key()
        s.must(s.client.post('/api/collaboration/work/' + goal_id + '/control', json={
            'action': 'resume', 'expected_version': latest['goal']['version'], 'idempotency_key': resume_key}))
        resumed_input = s.must(s.client.get(messages_path, params={
            'client_message_id': 'work-input:' + resume_key}))['messages']
        assert len(resumed_input) == 1
        context = native_goal(s, identity, session, goal_id)['room_work_context']
        continued = mcp(s, 'collaboration', {'action': 'room_work_start', **identity,
            **native_source(s, identity, resumed_input[0]['id']), **native_proof(s, identity, session),
            'target_project_id': s.project['id'], 'capabilities': ['read', 'write', 'execute'],
            'execution_targets': ['project_agent'], 'acceptance': 'Write and verify the second durable result once.',
            'coordinator_epoch': context['coordinator_epoch'],
            'confirm_task': True, 'idempotency_key': key()})
        assert continued['goal_id'] == goal_id and continued['scheduled']
        second_item = mcp(s, continued['next_request']['tool'], continued['next_request']['arguments'])['work_item']
        assert second_item['id'] != first_item['id']
        written = mcp(s, 'collaboration_work_execute', {
            **native_work_lease(s, identity, session, goal_id, second_item),
            'tool': 'write', 'arguments': {'path': 'native-second-input.txt',
                'content': 'Explicit continuation of the original Goal.\n', 'expected_sha256': 'new'},
            'idempotency_key': key()})
        assert s.poll(written['operation_id'], timeout=15)['state'] == 'succeeded'
        second_code = ("from pathlib import Path; "
            "assert Path('native-second-input.txt').read_text() == 'Explicit continuation of the original Goal.\\n'; "
            "f=Path('native-second-effects').open('a'); f.write('1'); f.close(); "
            "Path('native-second-finished').write_text('verified')")
        second = mcp(s, 'collaboration_work_execute', {
            **native_work_lease(s, identity, session, goal_id, second_item), 'tool': 'exec',
            'arguments': {'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(second_code),
                'resources': [{'kind': 'path', 'mode': 'write', 'name': '.'}],
                'timeout_seconds': 30, 'yield_seconds': 0}, 'idempotency_key': key()})
        assert s.poll(second['operation_id'], timeout=15)['state'] == 'succeeded'
        result = mcp(s, 'collaboration_work_result', {
            **native_work_lease(s, identity, session, goal_id, second_item), 'outcome': 'succeeded',
            'summary': 'The original operation was cancelled; the explicit continuation wrote and verified one result.',
            'operation_ids': [written['operation_id'], second['operation_id']], 'idempotency_key': key()})
        assert result['work_item']['state'] == 'succeeded'
        context = native_goal(s, identity, session, goal_id)['room_work_context']
        requirements = context['publication_requirements']
        publication = {'action': 'room_reply', **identity, **native_proof(s, identity, session),
            **{field: requirements[field] for field in ('source_message_id', 'source_version',
                'source_digest', 'expected_intent_revision', 'artifact_refs')},
            'coordinator_epoch': context['coordinator_epoch'], 'complete': True, 'outcome': 'succeeded',
            'body_text': '已复核原取消回执，并在同一目标中完成一次后续文件写入与验证。', 'idempotency_key': key()}
        final = mcp(s, 'collaboration', publication)
        duplicate = mcp(s, 'collaboration', {**publication,
            **native_proof(s, identity, session), 'idempotency_key': key()})
        assert duplicate['message']['id'] == final['message']['id']
        assert (s.projectalpha / 'native-first-effects').read_text() == '1'
        assert (s.projectalpha / 'native-second-effects').read_text() == '1'
        assert (s.projectalpha / 'native-second-finished').read_text() == 'verified'
        assert rows(s, 'SELECT COUNT(*) AS n FROM coordination_goals')[0]['n'] == 1
        assert rows(s, 'SELECT COUNT(*) AS n FROM coordination_operations WHERE work_item_id=?',
                    (first_item['id'],))[0]['n'] == 1
        operation_ids = {operation['operation_id'] for operation in
                         native_goal(s, identity, session, goal_id)['operations']}
        assert operation_ids == {first['operation_id'], written['operation_id'], second['operation_id']}
        assert rows(s, 'SELECT COUNT(*) AS n FROM room_goal_publications WHERE goal_id=?',
                    (goal_id,))[0]['n'] == 1
        assert rows(s, 'SELECT COUNT(*) AS n FROM team_publications WHERE goal_id=?', (goal_id,))[0]['n'] == 1
        assert rows(s, 'SELECT id FROM room_members') == [{'id': identity['member_id']}]
        assert rows(s, 'SELECT id FROM mcp_event_subscriptions') == [{'id': subscription_id}]
        assert rows(s, 'SELECT id FROM dot_consumer_sessions') == [{'id': session['consumer_session_id']}]
        remaining = native_inbox(s, identity)
        assert not [item for item in remaining['items'] if item['kind'] == 'message']
        assert not [item for item in remaining['items'] if item['kind'] == 'work_ready' and item['ready']]
        notices = [item['id'] for item in remaining['items'] if item['kind'] not in {'message', 'work_ready'}]
        if notices:
            mcp(s, 'collaboration', {'action': 'room_inbox_ack', **identity,
                **native_proof(s, identity, session), 'entry_ids': notices,
                'idempotency_key': key()})
        ended = mcp(s, 'collaboration', {'action': 'room_consumer_yield', **identity,
            **native_proof(s, identity, session), 'idempotency_key': key()})
        assert ended['state'] == 'yielded' and not ended['more']
        assert s.agent.pid == agent_pid and len(set(s.hub_pids)) == 2
        print(json.dumps({'scenario': 'native_room_real_agent_restart_stop_continue',
            'hub_processes': 2, 'agent_process_survived': True,
            'member_and_subscription_preserved': True, 'original_goal_preserved': True,
            'original_operation_preserved': True, 'real_side_effect_counts': [1, 1],
            'canonical_publications': 1, 'message_http_seconds': round(message_seconds, 4),
            'stop_http_seconds': round(stop_seconds, 4),
            'latency_observations': 'single local requests, not a load-test percentile',
            'real_dot_host': False}))


def serve_fixture_hub(directory, port, callback_port, receipt):
    import uvicorn
    from hub.app import create_app
    from hub.collaboration.network import Reply, retry_after_seconds

    from shared.util import atomic_json
    from tests.fixture_hub import bind_listener

    async def local_callback(url, body, headers):
        parsed = urlsplit(url)
        assert parsed.scheme == 'https' and parsed.hostname == 'process-fixture.example.invalid'
        async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
            reply = await client.post(f'http://127.0.0.1:{callback_port}' + parsed.path,
                                      content=body, headers=headers)
        delay = (retry_after_seconds(reply.headers.get('Retry-After'))
                 if reply.status_code in {429, 503} else None)
        return Reply(reply.status_code, reply.content, delay)

    with bind_listener(port) as listener:
        port = listener.getsockname()[1]
        os.environ['HUB_PORT'] = str(port)
        for key in ('HUB_PUBLIC_URL', 'MCP_PUBLIC_URL'):
            if os.environ.get(key) in {None, '', 'http://127.0.0.1:0'}:
                os.environ[key] = f'http://127.0.0.1:{port}'
        app = create_app(directory)
        app.state.runtime.collaboration.events.sender = local_callback
        server = uvicorn.Server(uvicorn.Config(
            app, host='127.0.0.1', port=port, log_level='warning'))
        atomic_json(Path(receipt), {'port': port})
        server.run(sockets=[listener])


if __name__ == '__main__':
    assert len(sys.argv) == 6 and sys.argv[1] == '--hub'
    serve_fixture_hub(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
