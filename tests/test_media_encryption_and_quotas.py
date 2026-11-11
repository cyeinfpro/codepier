"""Synthetic payload compatibility, tenant fairness, retention and rekey checks."""
import base64
import hashlib
import sqlite3
import threading

import pytest

from hub.collaboration import media_cache
from hub.keyring import rotate_key
from hub.store import Store
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_room_members_rebuild import rooms as rooms, create
from tests.test_dot_telegram_http import telegram_http as telegram_http


def insert_attachment(store, identifier, owner='alice', size=3, content=None):
    store.execute('''INSERT INTO channel_attachments
        (id,space_id,owner_user_id,room_id,conversation_id,project_id,message_id,ordinal,direction,
         binding_id,generation,peer_id,kind,name,mime,size,sha256,state,private_source,content,expires_at,created,updated)
        VALUES (?,'space',?,'','','',?,0,'inbound','',0,'','file','test','text/plain',?,?,'ready','',?,100,0,0)''',
        (identifier, owner, identifier, size, hashlib.sha256(b'abc').hexdigest(), content))


def test_binary_codec_has_single_base64_and_reads_legacy_and_ranges(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        data = bytes(range(256)) * 1024
        encoded = media_cache.encode(store, data)
        assert len(encoded) < len(data) * 1.35
        assert media_cache.decode(store, encoded) == data
        assert media_cache.decode(store, store.encrypt(base64.b64encode(data).decode())) == data
        row = dict(state='ready', content=encoded, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        part, _, start, end, partial = media_cache.decode_range(store, row, 'bytes=15-30')
        assert (part, start, end, partial) == (data[15:31], 15, 30, True)
        with pytest.raises(DevError, match='完整性'):
            media_cache.decode_range(store, {**row, 'sha256': '0' * 64})
    finally:
        store.close()


def test_owner_cap_counts_pending_chunks_without_starving_other_owner(tmp_path, monkeypatch):
    store = Store(tmp_path / 'hub')
    try:
        monkeypatch.setattr(media_cache, 'PER_OWNER_CACHE', 10)
        monkeypatch.setattr(media_cache, 'MAX_CACHE', 100)
        insert_attachment(store, 'a', size=8)
        insert_attachment(store, 'b', owner='bob', size=2)
        store.execute("INSERT INTO channel_attachment_chunks VALUES ('a',0,2,'digest','cipher')")
        alice = {'space_id': 'space', 'owner_user_id': 'alice'}
        bob = {'space_id': 'space', 'owner_user_id': 'bob'}
        assert media_cache.usage(store, **alice) == 10
        assert not media_cache.available(store, alice, 1)
        assert media_cache.available(store, bob, 5)
        assert media_cache.available(store, alice, 2, exclude_chunks='a')
        assert media_cache.usage(store) == 12
    finally:
        store.close()


def test_unattached_upload_expires_early_and_sent_upload_extends(rooms):
    service, owner, _, clock = rooms
    room = create(service, owner)
    aid = service.room_attachments.upload(room['id'], 'file.txt', b'abc', key(), owner)['attachment']['id']
    row = service.store.one('SELECT * FROM channel_attachments WHERE id=?', (aid,))
    assert row['expires_at'] == clock[0] + media_cache.UNATTACHED_TTL
    service.room_runtime.send({'conversation_id': room['id'], 'body_text': '', 'attachment_ids': [aid],
                              'client_message_id': key(), 'idempotency_key': key()}, owner)
    assert service.store.one('SELECT expires_at FROM channel_attachments WHERE id=?', (aid,))['expires_at'] == clock[0] + 7 * 86400


def test_raw_attachment_and_chunk_survive_key_rotation(tmp_path):
    directory = tmp_path / 'hub'
    store = Store(directory)
    raw = b'\x00\xff\x80binary'
    try:
        insert_attachment(store, 'a', content=media_cache.encode(store, raw))
        store.execute("INSERT INTO channel_attachment_chunks VALUES ('a',0,?,?,?)",
                      (len(raw), hashlib.sha256(raw).hexdigest(), media_cache.encode(store, raw)))
    finally:
        store.close()
    rotate_key(directory)
    store = Store(directory)
    try:
        for table in ('channel_attachments', 'channel_attachment_chunks'):
            value = store.one('SELECT content FROM ' + table)['content']
            assert media_cache.decode(store, value) == raw
        with sqlite3.connect(directory / 'hub.sqlite3') as db:
            assert b'binary' not in db.execute('SELECT content FROM channel_attachments').fetchone()[0].encode()
    finally:
        store.close()


@pytest.mark.parametrize('revoke_during_decode', [False, True])
def test_http_range_decodes_off_database_thread_and_rechecks_authority(telegram_http, monkeypatch, revoke_during_decode):
    app, client, _ = telegram_http
    media = app.state.runtime.collaboration.channels.media
    payload = b'abcdef'
    row = dict(id='synthetic', state='ready', content=media_cache.encode(app.state.store, payload),
               size=6, sha256=hashlib.sha256(payload).hexdigest(), name='file.txt')
    reads, decodes = [], []
    def authorized_row(identifier, actor):
        reads.append(threading.get_ident())
        if revoke_during_decode and len(reads) == 2:
            raise DevError('ATTACHMENT_NOT_FOUND', 'Scope revoked during download', 403)
        return row
    original = media.decode_range
    def decode_range(record, header=None):
        decodes.append(threading.get_ident())
        return original(record, header)
    monkeypatch.setattr(media, 'row', authorized_row)
    monkeypatch.setattr(media, 'decode_range', decode_range)
    response = client.get('/api/collaboration/attachments/synthetic/content', headers={'Range': 'bytes=1-3'})
    assert len(reads) == 2 and reads[0] == reads[1]
    assert decodes and decodes[0] != reads[0]
    if revoke_during_decode:
        assert response.status_code == 403
        assert response.content != b'bcd'
    else:
        assert response.status_code == 206 and response.content == b'bcd'
        assert response.headers['content-range'] == 'bytes 1-3/6'


@pytest.mark.parametrize('error_code,status,expected', [
    ('ATTACHMENT_CACHE_QUOTA', 429, 'pending'),
    ('UPSTREAM_BUSY', 503, 'unavailable'),
    ('ATTACHMENT_HASH_MISMATCH', 409, 'unavailable'),
])
def test_weixin_quota_pressure_does_not_permanently_exhaust_retries(tmp_path, error_code, status, expected):
    from types import SimpleNamespace
    from hub.collaboration.channel_media import ChannelMedia
    store = Store(tmp_path / 'hub')
    try:
        insert_attachment(store, 'pressure', content=None)
        store.execute("UPDATE channel_attachments SET state='fetching', attempts=5,lease_owner='worker',lease_fence=1 WHERE id='pressure'")
        media = SimpleNamespace(store=store, c=SimpleNamespace(clock=lambda: 1000))
        claim = dict(id='pressure', lease_owner='worker', lease_fence=1)
        ChannelMedia.failed(media, claim, DevError(error_code, 'synthetic failure', status))
        row = store.one("SELECT * FROM channel_attachments WHERE id='pressure'")
        assert row['state'] == expected
        assert row['reason_code'] == error_code
        assert row['next_attempt'] > 1000 and row['lease_until'] == 0
        assert row['attempts'] == 5
    finally:
        store.close()
