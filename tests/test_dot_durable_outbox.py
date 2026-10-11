"""Host adapter fault injection against real in-process Dot receipt transactions."""
import asyncio
import json

from cryptography.fernet import Fernet
import pytest

from shared.dot_outbox import DotOutbox
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, owner_message


def setup(fixture, tmp_path, **limits):
    c = duplex(fixture)
    source, _ = owner_message(c)
    cipher = Fernet(Fernet.generate_key())
    kwargs = {'seal': lambda text: cipher.encrypt(text.encode()).decode(),
              'unseal': lambda text: cipher.decrypt(text.encode()).decode(), 'clock': lambda: c.clock[0], **limits}
    path = tmp_path / 'host-outbox' / 'publications.sqlite3'
    outbox = DotOutbox(path, **kwargs)
    identity = {'connection_id': 'fixture-connector', 'grant_id': c.actor.grant_id,
                'space_id': c.actor.space_id, 'owner_user_id': c.actor.user_id,
                'conversation_id': c.slot['conversation_id']}
    args = {**c.scope, 'dot_id': c.slot['id'], 'body_text': 'The complete preserved response: 实际结果与未完成项。',
            'reply_to_id': source['message']['id'], 'idempotency_key': key()}
    return c, outbox, identity, args, path, kwargs


def callbacks(c, *, after_send=None, received=None):
    async def authorize(envelope):
        c.s.dot_chat.scope(envelope['arguments'], c.actor,
            __import__('shared.collaboration_contracts', fromlist=['DotChatMessage']).DotChatMessage, connector=True)
        assert envelope['identity']['grant_id'] == c.actor.grant_id
        assert envelope['identity']['conversation_id'] == c.slot['conversation_id']

    async def lookup(envelope):
        args = envelope['arguments']
        return c.s.task_liveness.receipt({**c.scope, 'dot_id': c.slot['id'], 'kind': envelope['kind'],
                                        'request_key': args['idempotency_key']}, c.actor)

    async def send(envelope):
        if received is not None:
            received.append(envelope['arguments'])
        result = c.s.dot_chat.message(envelope['arguments'], c.actor)
        if after_send:
            after_send()
        return result
    return {'authorize': authorize, 'lookup': lookup, 'send': send}


def test_body_is_durable_encrypted_and_exact_key_is_immutable(collab, tmp_path):
    c, outbox, identity, args, path, kwargs = setup(collab, tmp_path)
    first = outbox.enqueue(identity, 'message', args)
    assert outbox.enqueue(identity, 'message', args) == first
    with pytest.raises(DevError):
        outbox.enqueue(identity, 'message', {**args, 'body_text': 'different'})
    with pytest.raises(DevError):
        outbox.enqueue({**identity, 'conversation_id': 'other'}, 'message', args)
    raw = outbox.db.execute('SELECT body FROM dot_outbox_entries').fetchone()[0]
    assert args['body_text'] not in raw and args['idempotency_key'] not in raw
    outbox.close()
    reopened = DotOutbox(path, **kwargs)
    try:
        result = asyncio.run(reopened.deliver(first['id'], **callbacks(c)))
        assert result['state'] == 'committed'
        assert reopened.list()['items'][0]['request_digest'] == first['request_digest']
    finally:
        reopened.close()


def test_lost_commit_response_recovers_without_second_send_or_new_key(collab, tmp_path):
    c, outbox, identity, args, path, kwargs = setup(collab, tmp_path)
    entry = outbox.enqueue(identity, 'message', args)
    sent = []

    def dropped():
        raise TimeoutError('synthetic loss after actual commit')

    try:
        result = asyncio.run(outbox.deliver(entry['id'], **callbacks(c, after_send=dropped, received=sent)))
        assert result['state'] == 'checking_receipt'
        c.clock[0] += 6
        recovered = asyncio.run(outbox.deliver(entry['id'], **callbacks(c, received=sent)))
        assert recovered['state'] == 'committed' and len(sent) == 1
        assert sent[0]['idempotency_key'] == args['idempotency_key']
        assert not c.s.store.all('SELECT * FROM operations')
        assert len(c.s.store.all("SELECT id FROM collaboration_messages WHERE origin='bound_connector'")) == 1
    finally:
        outbox.close()


def test_unknown_receipt_cannot_trigger_send(collab, tmp_path):
    c, outbox, identity, args, path, kwargs = setup(collab, tmp_path)
    entry = outbox.enqueue(identity, 'message', args)
    sent = []
    cb = callbacks(c, received=sent)

    async def unknown(envelope):
        return {'committed': False, 'receipt_state': 'unknown', 'authoritative_lookup': False}

    try:
        result = asyncio.run(outbox.deliver(entry['id'], **{**cb, 'lookup': unknown}))
        assert result['state'] == 'checking_receipt' and not sent
    finally:
        outbox.close()


def test_wrong_digest_or_revocation_requires_attention(collab, tmp_path):
    c, outbox, identity, args, path, kwargs = setup(collab, tmp_path)
    first = outbox.enqueue(identity, 'message', args)
    cb = callbacks(c)

    async def wrong(envelope):
        return {'committed': True, 'request_digest': 'unrelated', 'message_id': 'other'}

    try:
        result = asyncio.run(outbox.deliver(first['id'], **{**cb, 'lookup': wrong}))
        assert result['state'] == 'attention'
        second = outbox.enqueue(identity, 'message', {**args, 'idempotency_key': key()})
        c.s.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (c.actor.grant_id,))
        result = asyncio.run(outbox.deliver(second['id'], **cb))
        assert result['state'] == 'attention'
        assert not c.s.store.all("SELECT id FROM collaboration_messages WHERE origin='bound_connector'")
    finally:
        outbox.close()


def test_single_flight_and_crash_fence_are_persistent(collab, tmp_path):
    c, outbox, identity, args, path, kwargs = setup(collab, tmp_path)
    first = outbox.enqueue(identity, 'message', args)
    second = outbox.enqueue(identity, 'message', {**args, 'idempotency_key': key()})
    original = outbox._claim(first['id'], 45)
    outbox.close()
    reopened = DotOutbox(path, **kwargs)
    try:
        assert not asyncio.run(reopened.deliver(second['id'], **callbacks(c)))['claimed']
        c.clock[0] += 46
        fresh = reopened._claim(first['id'], 45)
        assert fresh['claim_fence'] > original['claim_fence']
        with pytest.raises(DevError, match='世代已失效'):
            reopened._state(original, 'committed')
        c.clock[0] += 46
        assert asyncio.run(reopened.deliver(first['id'], **callbacks(c)))['state'] == 'committed'
    finally:
        reopened.close()


def test_quota_retention_and_cleanup_never_delete_unconfirmed_body(collab, tmp_path):
    c, outbox, identity, args, path, kwargs = setup(collab, tmp_path, max_entries=1)
    entry = outbox.enqueue(identity, 'message', args, retention_seconds=60)
    try:
        with pytest.raises(DevError, match='已满'):
            outbox.enqueue(identity, 'message', {**args, 'idempotency_key': key()})
        c.clock[0] += 61
        asyncio.run(outbox.deliver(entry['id'], **callbacks(c)))
        assert outbox.list()['items'][0]['error_code'] == 'RETENTION_EXPIRED'
        with pytest.raises(DevError):
            outbox.purge_committed([entry['id']], expected_digests={entry['id']: entry['request_digest']})
        assert json.loads(kwargs['unseal'](outbox.db.execute('SELECT body FROM dot_outbox_entries').fetchone()[0]))['arguments']['body_text'] == args['body_text']
    finally:
        outbox.close()
