"""Synthetic rekey coverage for actual consumer rows and mixed delivery bodies."""
import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

from cryptography.fernet import Fernet, InvalidToken
import pytest

from hub.collaboration.common import MESSAGE_EVENT, canonical
from hub.collaboration.config import CollaborationConfig
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from hub.collaboration.service import CollaborationService
from hub.keyring import rotate_key
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from tests.collaboration_support import key
from tests.legacy_iam_fixture import seed_owner, seed_grant


async def fixture_data(directory):
    store = Store(directory)
    now = time.time()
    seed_owner(store, 'owner', 'admin')
    seed_grant(store, 'broad', 'owner', scopes=('read', 'write', 'execute'), projects=('proj',))
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,?)",
                  (store.encrypt('synthetic-device'), now))
    store.execute("""INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created)
                     VALUES ('proj','proj','proj','dev','/tmp/fixture','write',1,?)""", (now,))
    runtime = Runtime(store)
    config = replace(CollaborationConfig(enabled=True), events_enabled=True)
    service = CollaborationService(runtime, config, clock=lambda: now)
    runtime.collaboration = service

    async def receiver(url, body, headers):
        payload = json.loads(body)
        return Reply(200, canonical({'challenge': payload['challenge']}).encode()) if payload.get('type') == 'verification' else Reply(204)

    service.events = EventService(service, receiver)
    owner = Principal('panel:admin', 'owner', {'read', 'write'}, ['*'], admin=True)
    actor = Principal('mcp:broad:fixture', 'owner', {'read', 'write', 'execute'}, ['proj'], grant_id='broad')
    scope = {'project': 'proj', 'environment_id': 'production'}
    room = service.room_create({**scope, 'idempotency_key': key()}, owner)['room']
    dot = service.dots.create({**scope, 'conversation_id': room['id'], 'label': 'rekey fixture',
        'duplex': True, 'capabilities': ['read'], 'confirm_tasks': True, 'idempotency_key': key()}, owner)['dot']
    service.joining.join({'code': dot['join_code'], 'idempotency_key': key()}, actor)
    config_reply = service.dot_mailbox.configure({**scope, 'dot_id': dot['id'], 'protocol_version': 2,
        'confirm_consumer_protocol': True, 'idempotency_key': key()}, actor)
    subscribed = await service.events.subscribe({**config_reply['subscription_request'],
        'delivery': {'mode': 'webhook', 'url': 'https://callback.example.invalid/rekey',
                     'secret': 'whsec_eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg='}}, actor)
    sub = store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (subscribed['id'],))
    binding = service.dot_mailbox.binding(dot['id'])
    ticket = service.dot_consumers.issue_ticket(room, dot, sub, binding['generation'], key())
    raw = {**scope, 'dot_id': dot['id'], 'binding_generation': binding['generation'],
           'wake_ticket': ticket, 'consumer_run_id': key(), 'idempotency_key': key()}
    session = service.dot_consumers.begin(raw, actor)
    message = service.chatroom.create({**scope, 'room_id': room['id'], 'conversation_id': room['id'],
        'body_text': 'synthetic input', 'mentions': [{'slot_id': dot['id']}],
        'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
    body = canonical({'event_id': 'original-event', 'wake_ticket': ticket})
    legacy = '{"event_id":"legacy-plaintext"}'
    with store.transaction():
        service.emit(room, MESSAGE_EVENT, 'legacy-fixture', 1, {
            'conversation_id': room['id'], 'room_id': room['id'], 'message_id': message['id'],
            'message_version': 1, 'recipient_slot_id': dot['id'], 'thread_root_id': message['id']},
            grant_id='broad')
        events = store.all('SELECT id,seq FROM mcp_event_outbox ORDER BY seq LIMIT 2')
        assert len(events) == 2
        for identifier, event, value in [('v2', events[0], 'sealed:v1:' + store.encrypt(body)),
                                         ('v1', events[1], legacy)]:
            store.execute("""INSERT INTO mcp_event_deliveries
                (id,subscription_id,event_id,event_seq,body,created) VALUES (?,?,?,?,?,?)""",
                (identifier, sub['id'], event['id'], event['seq'], value, now))
    store.close()
    return {'raw': raw, 'actor': actor, 'config': config, 'now': now,
            'session': session, 'body': body, 'legacy': legacy, 'receiver': receiver}


@pytest.mark.asyncio
async def test_rotation_reopens_exact_begin_proof_ticket_and_retry_body(tmp_path):
    directory = tmp_path / 'hub'
    data = await fixture_data(directory)
    previous = (directory / 'master.key').read_bytes()
    for _ in range(2):
        receipt = rotate_key(directory)
        assert receipt['records'] >= 3
        assert (directory / 'master.key').read_bytes() != previous
        previous = (directory / 'master.key').read_bytes()
        store = Store(directory)
        try:
            service = CollaborationService(Runtime(store), data['config'], clock=lambda: data['now'])
            service.events = EventService(service, data['receiver'])
            replay = service.dot_consumers.begin(data['raw'], data['actor'])
            assert replay['consumer_proof'] == data['session']['consumer_proof']
            assert replay['lease_until'] == data['session']['lease_until']
            ticket = store.one('SELECT ticket_secret FROM dot_wake_tickets')['ticket_secret']
            retry = store.one("SELECT body FROM mcp_event_deliveries WHERE id='v2'")['body']
            assert ticket.startswith('cp1:' + receipt['key_id'] + ':')
            assert retry.startswith('sealed:v1:cp1:' + receipt['key_id'] + ':')
            assert store.decrypt(ticket) == data['raw']['wake_ticket']
            assert store.decrypt(retry[len('sealed:v1:'):]) == data['body']
            assert store.one("SELECT body FROM mcp_event_deliveries WHERE id='v1'")['body'] == data['legacy']
        finally:
            store.close()


@pytest.mark.parametrize('kind', ['proof', 'ticket', 'sealed_delivery', 'plaintext'])
def test_missing_key_distinguishes_new_ciphertext_from_legacy_json(tmp_path, kind):
    directory = tmp_path / kind
    directory.mkdir()
    # Exercise the startup key loader before migration, with only the storage
    # column under test. Older credential columns cannot hide a missing guard.
    encrypted = Fernet(Fernet.generate_key()).encrypt(b'synthetic-only-secret').decode()
    table, column = {'proof': ('dot_consumer_sessions', 'proof_secret'),
        'ticket': ('dot_wake_tickets', 'ticket_secret'),
        'sealed_delivery': ('mcp_event_deliveries', 'body'),
        'plaintext': ('mcp_event_deliveries', 'body')}[kind]
    value = '{"legacy":true}' if kind == 'plaintext' else (
        'sealed:v1:' + encrypted if kind == 'sealed_delivery' else encrypted)
    with sqlite3.connect(directory / 'hub.sqlite3') as db:
        db.execute(f'CREATE TABLE {table} ({column} TEXT)')
        db.execute(f'INSERT INTO {table} VALUES (?)', (value,))
        state = SimpleNamespace(state_directory=directory, db=db)
        if kind == 'plaintext':
            cipher = Store._load_cipher(state)
            assert cipher.decrypt(cipher.encrypt(b'new')) == b'new'
            assert (directory / 'master.key').is_file()
        else:
            with pytest.raises(RuntimeError, match='missing'):
                Store._load_cipher(state)
            assert not (directory / 'master.key').exists()


@pytest.mark.asyncio
async def test_corrupt_sealed_body_rolls_back_all_cipher_updates(tmp_path):
    directory = tmp_path / 'corrupt'
    await fixture_data(directory)
    with sqlite3.connect(directory / 'hub.sqlite3') as db:
        original = db.execute('SELECT proof_secret FROM dot_consumer_sessions').fetchone()[0]
        db.execute("UPDATE mcp_event_deliveries SET body='sealed:v1:invalid' WHERE id='v2'")
    with pytest.raises(InvalidToken):
        rotate_key(directory)
    with sqlite3.connect(directory / 'hub.sqlite3') as db:
        assert db.execute('SELECT proof_secret FROM dot_consumer_sessions').fetchone()[0] == original
        assert db.execute("SELECT body FROM mcp_event_deliveries WHERE id='v2'").fetchone()[0] == 'sealed:v1:invalid'
