"""Offline key rotation preserves live and staged host credentials exactly."""
import sqlite3
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest

from hub.collaboration.config import CollaborationConfig
from hub.collaboration.service import CollaborationService
from hub.keyring import rotate_key
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from tests.collaboration_support import key
from tests.legacy_iam_fixture import seed_grant, seed_owner
from tests.test_room_members_rebuild import add, create, identity, join, subscribe


@pytest.mark.asyncio
async def test_rotation_preserves_pending_invite_proof_nonce_and_live_route(tmp_path):
    directory = tmp_path / 'room-rekey'
    config = CollaborationConfig(enabled=True, events_enabled=True, weixin_enabled=False)
    store = Store(directory)
    seed_owner(store, 'owner', 'admin')
    seed_grant(store, 'worker', 'owner', scopes=('read',), projects=())
    owner = Principal('panel:admin', 'owner', {'read', 'write'}, ['*'], admin=True)
    worker = Principal('mcp:worker:fixture', 'owner', {'read'}, [], grant_id='worker')
    service = CollaborationService(Runtime(store), config)
    created = add(service, owner, create(service, owner))
    live, _ = join(service, worker, created)
    old_sub = await subscribe(service, worker, live, 'original')
    probe = service.rooms.probe_read({**identity(live), 'wake_ticket': live['wake_ticket'], 'idempotency_key': key()}, worker)
    service.rooms.probe_reply({**identity(live), 'probe_nonce': probe['probe_nonce'], 'idempotency_key': key()}, worker)
    flow = service.rooms.connect_read({'flow_id': created['flow']['id']}, owner)['flow']
    replacement = service.rooms.connect_action({'flow_id': flow['id'], 'action': 'rebind',
        'expected_version': flow['version'], 'idempotency_key': key()}, owner)
    pending, join_request = join(service, worker, replacement)
    await subscribe(service, worker, pending, 'replacement')
    read_request = {**identity(pending), 'wake_ticket': pending['wake_ticket'], 'idempotency_key': key()}
    nonce = service.rooms.probe_read(read_request, worker)['probe_nonce']
    candidate_id = service.rooms.pending.current(live['member_id'])['id']
    store.close()

    for _ in range(2):
        receipt = rotate_key(directory)
        store = Store(directory)
        service = CollaborationService(Runtime(store), config)
        try:
            candidate = store.one('SELECT * FROM room_pending_bindings WHERE id=?', (candidate_id,))
            for column in ('invite_secret', 'connection_proof_secret', 'probe_nonce_secret'):
                assert candidate[column].startswith('cp1:' + receipt['key_id'] + ':')
            assert store.decrypt(candidate['invite_secret']) == join_request['invite_code']
            assert service.rooms.join(join_request, worker)['connection_proof'] == pending['connection_proof']
            assert service.rooms.probe_read(read_request, worker)['probe_nonce'] == nonce
            assert service.rooms.member_scope(worker, **identity(live), require_proof=True)[2]['subscription_id'] == old_sub['id']
            assert store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (old_sub['id'],))['state'] == 'active'
            assert not store.all('PRAGMA foreign_key_check')
        finally:
            store.close()

    store = Store(directory)
    service = CollaborationService(Runtime(store), config)
    try:
        switched = service.rooms.probe_reply({**identity(pending), 'probe_nonce': nonce, 'idempotency_key': key()}, worker)
        assert switched['binding_activated'] and switched['member']['state'] == 'ready_chat'
        assert store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (old_sub['id'],))['state'] == 'unsubscribed'
    finally:
        store.close()


@pytest.mark.parametrize('column', ['invite_secret', 'connection_proof_secret', 'probe_nonce_secret'])
def test_missing_master_key_never_discards_pending_binding_ciphertext(tmp_path, column):
    directory = tmp_path / column
    directory.mkdir()
    encrypted = Fernet(Fernet.generate_key()).encrypt(b'synthetic-staged-credential').decode()
    with sqlite3.connect(directory / 'hub.sqlite3') as db:
        db.execute(f'CREATE TABLE room_pending_bindings ({column} TEXT)')
        db.execute(f'INSERT INTO room_pending_bindings VALUES (?)', (encrypted,))
        with pytest.raises(RuntimeError, match='missing'):
            Store._load_cipher(SimpleNamespace(state_directory=directory, db=db))
        assert not (directory / 'master.key').exists()
