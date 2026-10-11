"""Cross-feature acceptance edges; all artifacts, identities and provider calls are fixtures."""
import asyncio
import json

import pytest

from hub.collaboration.weixin_protocol import WeixinClient, WireReply, approved_url, API_BASE
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex
from tests.test_dot_channel_shares import approved, artifact
from tests.test_dot_room_scope import setup as scope_setup


def test_dot_creation_explicit_budget_reaches_immutable_delegation_policy(collab):
    c = duplex(collab, operation_timeout_seconds=1800)
    dot = c.s.dots.find(c.slot['id'])
    assert json.loads(dot['setup'])['operation_timeout_seconds'] == 1800
    policy = c.s.dots.policy(c.room, dot)
    assert json.loads(policy['spec'])['budget']['operation_timeout_seconds'] == 1800
    assert c.s.dots.slot_view(c.room, c.s.joining.slot(c.room, dot['id']), dot, c.owner)['operation_timeout_seconds'] == 1800


def test_second_attachment_definite_failure_does_not_drop_third_or_resend_caption(collab):
    c, w, account, binding, scope, source, args = approved(collab)
    extra = w.media.approve({**scope, 'source_message_id': source, 'expected_source_version': 1,
        'artifact_ids': [artifact(c, 'third.txt')], 'confirm_file_egress': True, 'idempotency_key': key()}, c.owner)
    args['attachment_approval_ids'] += extra['attachment_approval_ids']
    result = c.s.invoke('collaboration', args, c.actor)
    attachments = result['message']['attachments']
    assert [r['name'] for r in attachments] == ['first.txt', 'second.png', 'third.txt']
    for index, item in enumerate(attachments):
        claim = w.media.claim(item['id'])
        if index == 1:
            w.media.failed(claim, DevError('WEIXIN_MEDIA_LIMIT', 'synthetic definite file failure', 413))
        else:
            w.media.commit(claim, b'synthetic file only')
    sent = []
    class Provider:
        async def upload_media(self, peer, content, kind, name, **identity):
            assert content == b'synthetic file only'
            return {'type': 4, 'file_item': {'file_name': name}}
        async def send(self, peer, context, client_id, items):
            sent.append((client_id, items))
            return {'provider_state': 'accepted', 'delivered_verified': False, 'read_verified': False}
    w.client_factory = lambda *values: Provider()
    parts = c.s.store.all('SELECT * FROM weixin_parts ORDER BY ordinal')
    for part in parts:
        asyncio.run(w.delivery.send_once(part['id']))
    assert [r['state'] for r in c.s.store.all('SELECT state FROM weixin_parts ORDER BY ordinal')] == ['accepted', 'accepted', 'rejected', 'accepted']
    assert len(sent) == 3 and sum(items[0]['type'] == 1 for _, items in sent) == 1
    assert sent[-1][1][0]['file_item']['file_name'] == 'third.txt'
    assert c.s.store.one('SELECT state FROM weixin_deliveries')['state'] == 'needs_attention'
    assert not c.s.store.all('SELECT * FROM operations')


def test_room_scope_rechecks_new_overlap_after_approval(collab):
    s, owner, actor, room, args = scope_setup(collab)
    s.room_scope.set(args, owner)
    s.store.execute("UPDATE projects SET root='/tmp/dot-scope-fixture/A/new-private' WHERE id='otherproj'")
    with pytest.raises(DevError, match='新增'):
        s.runtime._invoke('read', {'project': 'proj', 'path': 'README.md'}, actor)
    assert not s.store.all('SELECT * FROM operations')


def test_weixin_voice_and_unknown_encryption_fail_before_any_network_request():
    calls = []
    async def transport(*args, **kwargs):
        calls.append(args)
        return WireReply(200, b'{"ret":0}')
    client = WeixinClient(token='synthetic-token', transport=transport)
    with pytest.raises(DevError):
        asyncio.run(client.upload_media('peer', b'data', 'voice', 'audio.dat', filekey='file', key=b'x' * 16))
    with pytest.raises(DevError):
        asyncio.run(client.download_media({'encrypt_type': 9, 'full_url': 'https://attacker.invalid/file'}))
    assert calls == []
    for path in ['/ilink/../private', '/%2e%2e/private']:
        with pytest.raises(DevError):
            approved_url(API_BASE + path, bases=(API_BASE,))


def test_boolean_ret_is_not_success_and_opaque_provider_receipt_is_preserved():
    async def false_success(*args, **kwargs):
        return WireReply(200, b'{"ret":false}')
    with pytest.raises(DevError):
        asyncio.run(WeixinClient(token='synthetic', transport=false_success).send('peer', 'context', 'id', [{'type': 1}]))
    async def accepted(*args, **kwargs):
        return WireReply(200, b'{"ret":0,"message_id":"opaque-provider-receipt"}')
    result = asyncio.run(WeixinClient(token='synthetic', transport=accepted).send('peer', 'context', 'id', [{'type': 1}]))
    assert result['provider_message_id'] == 'opaque-provider-receipt'
    assert result['delivered_verified'] is False
