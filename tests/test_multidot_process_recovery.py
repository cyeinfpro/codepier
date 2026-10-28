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
import shlex
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest

from tests.collaboration_support import key
from tests.support import BASE, Stack, wait_for, wait_for_hub

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
        self.hub = subprocess.Popen(
            [sys.executable, '-m', 'tests.test_multidot_process_recovery',
             '--hub', str(self.hubdir), str(self.port), str(self.callback_port)],
            cwd=BASE, env=self.env, stdout=self.hub_log, stderr=subprocess.STDOUT)
        self.hub_pids.append(self.hub.pid)
        wait_for_hub(self.hub, self.url)

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


def serve_fixture_hub(directory, port, callback_port):
    import uvicorn
    from hub.app import create_app
    from hub.collaboration.network import Reply, retry_after_seconds

    app = create_app(directory)

    async def local_callback(url, body, headers):
        parsed = urlsplit(url)
        assert parsed.scheme == 'https' and parsed.hostname == 'process-fixture.example.invalid'
        async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
            reply = await client.post(f'http://127.0.0.1:{callback_port}' + parsed.path,
                                      content=body, headers=headers)
        delay = (retry_after_seconds(reply.headers.get('Retry-After'))
                 if reply.status_code in {429, 503} else None)
        return Reply(reply.status_code, reply.content, delay)

    app.state.runtime.collaboration.events.sender = local_callback
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')


if __name__ == '__main__':
    assert len(sys.argv) == 5 and sys.argv[1] == '--hub'
    serve_fixture_hub(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
