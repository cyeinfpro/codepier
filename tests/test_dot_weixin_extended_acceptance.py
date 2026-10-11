"""Additional W01/W03/W14 fixtures; no actual Weixin account or user file access."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import pytest

from hub.artifacts import CHUNK
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, inbox, send_dot
from tests.test_dot_channel_shares import artifact
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet


def test_timestamped_upstream_revision_is_appended_without_losing_prior_history(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    created = 1700000000000
    initial = packet(200, 'first version', create_time_ms=created, update_time_ms=0)
    first = ingest(c, w, account, initial)['message_ids'][0]
    revision = packet(200, 'corrected version', create_time_ms=created, update_time_ms=created + 1000,
                      context_token='corrected-context')
    second = ingest(c, w, account, revision)['message_ids'][0]
    assert first != second
    original = c.s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (first,))
    corrected = c.s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (second,))
    assert json.loads(original['body'])['body_text'] == 'first version'
    assert json.loads(corrected['body'])['body_text'] == 'corrected version'
    assert corrected['thread_root_id'] == first
    assert json.loads(corrected['body'])['external_origin']['supersedes_message_id'] == first
    assert ingest(c, w, account, initial)['message_ids'] == [first]
    assert ingest(c, w, account, revision)['message_ids'] == [second]
    assert len(c.s.store.all("SELECT id FROM collaboration_messages WHERE origin='external_channel'")) == 2
    assert c.s.store.decrypt(c.s.store.one('SELECT token FROM weixin_contexts')['token']) == 'corrected-context'
    claim = w.claim_poll(account['account_id'])
    with pytest.raises(DevError, match='不同内容'):
        w.commit_updates(claim, packet(200, 'conflict at identical version', create_time_ms=created, update_time_ms=created + 1000))
    assert all(i['task_request'] is None for i in inbox(c)['items'])
    assert not c.s.store.all('SELECT * FROM operations')


@pytest.mark.parametrize('blocked_backlog', [False, True])
def test_two_projects_two_bots_same_peer_and_provider_id_never_cross_lanes(collab, blocked_backlog):
    s = collab[0]
    s.store.execute("UPDATE grants SET projects='[\"proj\",\"otherproj\"]' WHERE id='broad'")
    c1, w, a1, b1, scope1 = fixture_channel(collab)
    second_scope = {**c1.scope, 'project': 'otherproj'}
    second_room = s.room_create({**second_scope, 'idempotency_key': key()}, c1.owner)['room']
    second_fixture = list(collab)
    second_fixture[4], second_fixture[7] = second_room, second_scope
    c2 = duplex(tuple(second_fixture))
    scope2 = {**second_scope, 'conversation_id': c2.slot['conversation_id']}
    a2 = w.import_account({**scope2, 'bot_id': 'synthetic-bot-B', 'token': 'synthetic-token-B',
        'label': 'second fixture bot', 'confirm_credential_storage': True, 'confirm_provider_terms': True,
        'idempotency_key': key()}, c2.owner)
    b2 = w.bind({**scope2, 'account_id': a2['account_id'], 'dot_id': c2.slot['id'],
        'confirm_external_data_ingress': True, 'allow_replies': True, 'idempotency_key': key()}, c2.owner)
    w.commit_updates(w.claim_poll(a2['account_id']), packet(1, b2['pairing_challenge'], to_user_id='synthetic-bot-B'))
    pending = w.binding(b2['binding_id'], c2.owner, c2.room)
    w.control({**scope2, 'binding_id': pending['id'], 'expected_version': pending['version'],
        'action': 'activate', 'confirm_peer_id': 'synthetic-peer', 'idempotency_key': key()}, c2.owner)
    c1.clock[0] += 3
    claim1, claim2 = w.claim_poll(a1['account_id']), w.claim_poll(a2['account_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(w.commit_updates, claim1, packet(2, 'room A only', context_token='context-A'))
        f2 = pool.submit(w.commit_updates, claim2, packet(2, 'room B only', to_user_id='synthetic-bot-B', context_token='context-B'))
        m1, m2 = f1.result()['message_ids'][0], f2.result()['message_ids'][0]
    assert m1 != m2 and inbox(c1)['items'][0]['message_id'] == m1 and inbox(c2)['items'][0]['message_id'] == m2
    assert inbox(c1)['items'][0]['message']['body_text'] == 'room A only'
    assert inbox(c2)['items'][0]['message']['body_text'] == 'room B only'
    send_dot(c1, 'reply A', m1)
    if blocked_backlog:
        c1.s.store.execute("UPDATE weixin_parts SET state='unknown'")
        for number in range(16):
            c1.clock[0] += 1
            send_dot(c1, 'blocked reply A ' + str(number), m1)
        c1.clock[0] += 1
    send_dot(c2, 'reply B', m2)
    seen = []
    class Provider:
        def __init__(self, token):
            self.token = token
        async def send(self, peer, context, client_id, items):
            seen.append((self.token, peer, context, items[0]['text_item']['text']))
            return {'provider_state': 'accepted', 'delivered_verified': False, 'read_verified': False}
    w.client_factory = lambda base, token: Provider(token)
    asyncio.run(w.delivery.tick())
    expected = {('synthetic-token-B', 'synthetic-peer', 'context-B', 'reply B')}
    if not blocked_backlog:
        expected.add(('synthetic-weixin-secret-not-real', 'synthetic-peer', 'context-A', 'reply A'))
    assert set(seen) == expected
    if blocked_backlog:
        asyncio.run(w.delivery.tick())
        assert set(seen) == expected and len(seen) == 1
        assert s.store.one("SELECT COUNT(*) AS n FROM weixin_parts WHERE state='unknown'")['n'] == 1
        assert s.store.one("SELECT COUNT(*) AS n FROM weixin_parts WHERE state='queued'")['n'] == 16
    assert not s.store.all('SELECT * FROM operations')


def test_offline_artifact_resumes_original_chunk_offset_without_regeneration(collab, monkeypatch):
    c, w, account, binding, scope = fixture_channel(collab, media=True)
    source = ingest(c, w, account)['message_ids'][0]
    content = b'A' * CHUNK + b'B' * 71
    monkeypatch.setattr('hub.collaboration.channel_media.MAX_CACHE', len(content))
    original = artifact(c, 'original-proof.bin', content=content)
    approved = w.media.approve({**scope, 'source_message_id': source, 'expected_source_version': 1,
        'artifact_ids': [original], 'confirm_file_egress': True, 'idempotency_key': key()}, c.owner)
    result = c.s.invoke('collaboration', {'action': 'dot_message_v2', **c.scope,
        'dot_id': c.slot['id'], 'reply_to_id': source, 'body_text': 'Existing original artifact.',
        'attachment_approval_ids': approved['attachment_approval_ids'], 'idempotency_key': key()}, c.actor)
    attachment = result['message']['attachments'][0]['id']
    offsets = []
    async def original_chunk(row, project, offset):
        assert row['id'] == original and row['sha256'] == hashlib.sha256(content).hexdigest()
        offsets.append(offset)
        if len(offsets) == 2:
            raise DevError('ARTIFACT_DEVICE_OFFLINE', 'synthetic disconnection after original first chunk', 503)
        return content[offset:offset + CHUNK]
    monkeypatch.setattr(c.s.runtime.artifacts, 'chunk', original_chunk)
    asyncio.run(w.media.materialize(attachment))
    assert c.s.store.one('SELECT state FROM channel_attachments WHERE id=?', (attachment,))['state'] == 'pending'
    assert len(c.s.store.all('SELECT * FROM channel_attachment_chunks')) == 1
    c.clock[0] += 31
    asyncio.run(w.media.materialize(attachment))
    assert offsets == [0, CHUNK, CHUNK]
    assert w.media.content(attachment, c.actor)[0] == content
    assert not c.s.store.all('SELECT * FROM channel_attachment_chunks')
    assert not c.s.store.all('SELECT * FROM operations')
    assert c.s.store.one('SELECT COUNT(*) AS n FROM artifacts')['n'] == 1


def media_fixture(fixture, count=3):
    c, w, account, binding, scope = fixture_channel(fixture, media=True)
    source = ingest(c, w, account)['message_ids'][0]
    content = b'12345678'
    artifacts = [artifact(c, str(i) + '.bin', content=content) for i in range(count)]
    receipt = w.media.approve({**scope, 'source_message_id': source, 'expected_source_version': 1,
        'artifact_ids': artifacts, 'confirm_file_egress': True, 'idempotency_key': key()}, c.owner)
    result = c.s.invoke('collaboration', {'action': 'dot_message_v2', **c.scope,
        'dot_id': c.slot['id'], 'reply_to_id': source, 'body_text': 'Approved files.',
        'attachment_approval_ids': receipt['attachment_approval_ids'], 'idempotency_key': key()}, c.actor)
    identifiers = [a['id'] for a in result['message']['attachments']]
    for order, identifier in enumerate(identifiers):
        c.s.store.execute('UPDATE channel_attachments SET created=? WHERE id=?', (order, identifier))
    return c, w, identifiers, content


def test_denied_media_does_not_starve_later_authorized_attachment(collab, monkeypatch):
    c, w, identifiers, content = media_fixture(collab)
    original = w.media.claim
    def claim(identifier):
        if identifier in identifiers[:2]:
            raise DevError('WEIXIN_BINDING_INACTIVE', 'Synthetic paused binding', 403)
        return original(identifier)
    monkeypatch.setattr(w.media, 'claim', claim)
    seen = []
    async def chunk(row, project, offset):
        seen.append(row['id'])
        return content[offset:]
    monkeypatch.setattr(c.s.runtime.artifacts, 'chunk', chunk)
    asyncio.run(w.media.tick())
    asyncio.run(w.media.tick())
    assert len(seen) == 1
    assert w.media.content(identifiers[2], c.actor)[0] == content
    for identifier in identifiers[:2]:
        row = c.s.store.one('SELECT * FROM channel_attachments WHERE id=?', (identifier,))
        assert row['state'] == 'pending' and row['reason_code'] == 'WEIXIN_BINDING_INACTIVE'
        assert c.clock[0] < row['next_attempt'] <= c.clock[0] + 300


def test_concurrent_media_chunks_enforce_global_quota_and_duplicate_is_free(collab, monkeypatch):
    c, w, identifiers, content = media_fixture(collab, 2)
    monkeypatch.setattr('hub.collaboration.channel_media.MAX_CACHE', 12)
    claims = [w.media.claim(identifier) for identifier in identifiers]
    def save(claim):
        try:
            w.media.save_chunk(claim, 0, content)
            return 'saved'
        except DevError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, claims))
    assert sorted(results) == ['ATTACHMENT_CACHE_QUOTA', 'saved']
    winner = claims[results.index('saved')]
    w.media.save_chunk(winner, 0, content)
    assert c.s.store.one('SELECT SUM(bytes) AS n FROM channel_attachment_chunks')['n'] == 8
    w.media.commit(winner, content)
    assert w.media.content(winner['id'], c.actor)[0] == content


def test_media_commit_counts_other_unfinished_chunks(collab, monkeypatch):
    c, w, identifiers, content = media_fixture(collab, 2)
    monkeypatch.setattr('hub.collaboration.channel_media.MAX_CACHE', 12)
    first, second = [w.media.claim(identifier) for identifier in identifiers]
    w.media.save_chunk(first, 0, content)
    with pytest.raises(DevError) as caught:
        w.media.commit(second, content)
    assert caught.value.code == 'ATTACHMENT_CACHE_QUOTA'
    assert c.s.store.one('SELECT state FROM channel_attachments WHERE id=?', (second['id'],))['state'] == 'pending'
