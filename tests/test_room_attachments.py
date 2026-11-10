"""Room uploads retain source identity, encrypted bytes and live visibility."""
import base64
import json

import pytest

from hub.collaboration.room_host_migration import HostBindingBackfill
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_dot_weixin_channel import packet
from tests.test_room_members_rebuild import rooms as rooms, add, create, identity, join
from tests.test_room_host_integration import finish_probe, independent as independent, native_subscribe
from tests.test_room_consumer_sessions import connect, verify
from tests.test_room_weixin_native import setup_channel


def test_owner_file_upload_is_idempotent_and_same_message_atomic(rooms):
    s, owner, worker, _ = rooms
    room = create(s, owner)
    content = '这是房主上传的说明。'.encode()
    request_key = key()
    result = s.room_attachments.upload(room['id'], '说明.txt', content, request_key, owner)
    identifier = result['attachment']['id']
    assert s.room_attachments.upload(room['id'], '说明.txt', content, request_key, owner)['attachment']['id'] == identifier
    with pytest.raises(DevError):
        s.room_attachments.upload(room['id'], '说明.txt', b'changed', request_key, owner)
    row = s.store.one('SELECT * FROM channel_attachments WHERE id=?', (identifier,))
    assert row['room_id'] == row['project_id'] == '' and row['conversation_id'] == room['id']
    assert base64.b64encode(content).decode() not in row['content']
    with pytest.raises(DevError):
        s.channels.media.content(identifier, worker)
    raw = {'conversation_id': room['id'], 'body_text': '', 'attachment_ids': [identifier],
           'client_message_id': key(), 'idempotency_key': key()}
    sent = s.room_runtime.send(raw, owner)
    assert sent['message']['attachments'][0]['id'] == identifier
    assert s.room_runtime.send(raw, owner)['message']['id'] == sent['message']['id']
    data, manifest, *_ = s.channels.media.content(identifier, owner)
    assert data == content and manifest['message_id'] == sent['message']['id']
    assert len(s.store.all('SELECT * FROM collaboration_messages WHERE conversation_id=?', (room['id'],))) == 1


def test_cross_room_upload_reuse_rolls_back_message_and_expiry_never_regenerates(rooms):
    s, owner, _, clock = rooms
    first, second = create(s, owner), create(s, owner)
    identifier = s.room_attachments.upload(first['id'], 'input.txt', b'original file', key(), owner)['attachment']['id']
    with pytest.raises(DevError):
        s.room_runtime.send({'conversation_id': second['id'], 'body_text': 'wrong room',
            'attachment_ids': [identifier], 'client_message_id': key(), 'idempotency_key': key()}, owner)
    assert not s.store.one('SELECT 1 FROM collaboration_messages WHERE conversation_id=?', (second['id'],))
    clock[0] += 7 * 86400 + 1
    with pytest.raises(DevError) as expired:
        s.channels.media.content(identifier, owner)
    assert expired.value.code == 'ATTACHMENT_EXPIRED'
    assert not s.store.all('SELECT * FROM coordination_goals')


@pytest.mark.asyncio
async def test_member_file_read_uses_current_room_identity_and_images_are_native_content(rooms):
    c = await connect(rooms)
    verify(c)
    text = c.s.room_attachments.upload(c.room['id'], 'readme.txt', b'abcdef', key(), c.owner)['attachment']['id']
    image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aVZYAAAAASUVORK5CYII=')
    picture = c.s.room_attachments.upload(c.room['id'], 'pixel.png', image, key(), c.owner)['attachment']['id']
    sent = c.s.room_runtime.send({'conversation_id': c.room['id'], 'member_id': c.joined['member_id'],
        'body_text': '请看这两个附件', 'attachment_ids': [text, picture],
        'client_message_id': key(), 'idempotency_key': key()}, c.owner)
    read = c.s.room_attachments.read({**identity(c.joined), 'attachment_id': text, 'offset': 1, 'limit': 3}, c.actor)
    assert read['text'] == 'bcd' and read['next_offset'] == 4 and not read['grants_authority']
    picture_read = c.s.room_attachments.read({**identity(c.joined), 'attachment_id': picture}, c.actor)
    assert picture_read['_mcp_media_content'][0]['type'] == 'image'
    assert base64.b64decode(picture_read['_mcp_media_content'][0]['data']) == image
    assert json.loads(c.s.store.one('SELECT body FROM collaboration_messages WHERE id=?',
                                  (sent['message']['id'],))['body'])['attachment_ids'] == [text, picture]
    with pytest.raises(DevError):
        c.s.room_attachments.read({**identity(c.joined), 'connection_proof': 'invalid-proof' * 4, 'attachment_id': text}, c.actor)


@pytest.mark.asyncio
async def test_bearer_attachment_read_ends_when_the_current_host_is_revoked(independent):
    service, owner, actor, _ = independent
    room = create(service, owner)
    member, _ = join(service, actor, add(service, owner, room))
    ticket = await native_subscribe(service, actor, member, b'a')
    result, _ = finish_probe(service, actor, member, ticket)
    identifier = service.room_attachments.upload(room['id'], 'source.txt', b'room source', key(), owner)['attachment']['id']
    service.room_runtime.send({'conversation_id': room['id'], 'body_text': 'Room input',
        'attachment_ids': [identifier], 'client_message_id': key(), 'idempotency_key': key()}, owner)
    assert service.channels.media.content(identifier, actor)[0] == b'room source'

    host = {'host_connection_id': result['member']['host_connection_id']}
    impact = service.room_connections.impact(host, owner)
    service.room_connections.revoke({**host, 'expected_authority_version': impact['authority_version'],
        'impact_digest': impact['impact_digest'], 'idempotency_key': key()}, owner)
    with pytest.raises(DevError) as revoked:
        service.channels.media.content(identifier, actor)
    assert revoked.value.code == 'ATTACHMENT_NOT_FOUND'
    assert service.channels.media.content(identifier, owner)[0] == b'room source'


@pytest.mark.asyncio
async def test_native_weixin_attachment_metadata_requires_a_current_host(collab):
    (service, owner, actor, clock, member, _), flow, *_ = await setup_channel(collab)
    HostBindingBackfill(service.store, clock=lambda: clock[0]).batch(apply=True)
    host_id = service.rooms.member(member['member_id'])['host_connection_id']
    assert host_id
    data = packet(2, create_time_ms=int(clock[0] * 1000), item_list=[
        {'type': 4, 'file_item': {'file_name': 'source.txt', 'media': {
            'aes_key': base64.b64encode(b'k' * 16).decode(),
            'encrypt_query_param': 'synthetic-cdn-reference', 'encrypt_type': 1}}},
    ])
    incoming = service.weixin.commit_updates(service.weixin.claim_poll(flow['account_id']), data)
    message_id = incoming['message_ids'][0]
    identifier = service.store.one('SELECT id FROM channel_attachments WHERE message_id=?', (message_id,))['id']
    assert service.channels.media.row(identifier, actor)['message_id'] == message_id

    host = {'host_connection_id': host_id}
    impact = service.room_connections.impact(host, owner)
    service.room_connections.revoke({**host, 'expected_authority_version': impact['authority_version'],
        'impact_digest': impact['impact_digest'], 'idempotency_key': key()}, owner)
    with pytest.raises(DevError) as revoked:
        service.channels.media.row(identifier, actor)
    assert revoked.value.code == 'ATTACHMENT_NOT_FOUND'
    assert service.channels.media.row(identifier, owner)['message_id'] == message_id
