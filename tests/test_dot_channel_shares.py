"""Atomic versioned publications and exact artifact approvals in synthetic rooms."""
import hashlib
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import inbox
from tests.test_dot_weixin_channel import fixture_channel, ingest, packet


def artifact(c, name='proof.txt', content=b'synthetic file only', project='proj'):
    row = c.s.store.one('SELECT * FROM projects WHERE id=?', (project,))
    identifier = key()
    record = {'id': identifier, 'project_id': project, 'device_id': row['device_id'],
        'grant_id': c.actor.grant_id, 'actor': c.actor.actor, 'root': row['root'], 'name': name,
        'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'created': c.clock[0],
        'expires': c.clock[0] + 3600, 'source_operation_id': '', 'space_id': c.actor.space_id,
        'owner_user_id': c.actor.user_id}
    c.s.store.execute('INSERT INTO artifacts (' + ','.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')', tuple(record.values()))
    return identifier


def approved(fixture):
    c, w, account, binding, scope = fixture_channel(fixture, media=True)
    source = ingest(c, w, account)['message_ids'][0]
    ids = [artifact(c, 'first.txt'), artifact(c, 'second.png')]
    raw = {**scope, 'source_message_id': source, 'expected_source_version': 1,
           'artifact_ids': ids, 'confirm_file_egress': True, 'idempotency_key': key()}
    receipt = w.media.approve(raw, c.owner)
    assert w.media.approve(raw, c.owner) == receipt
    args = {'action': 'dot_message_v2', **c.scope, 'dot_id': c.slot['id'],
            'reply_to_id': source, 'body_text': '两件附件完整记录；传输状态分别核对。',
            'attachment_approval_ids': receipt['attachment_approval_ids'], 'idempotency_key': key()}
    return c, w, account, binding, scope, source, args


def test_two_files_publish_atomically_and_same_receipt_never_duplicates(collab):
    c, w, account, binding, scope, source, args = approved(collab)
    assert inbox(c)['items'][0]['reply_v2_arguments']['attachment_approval_ids'] == args['attachment_approval_ids']
    result = c.s.invoke('collaboration', args, c.actor)
    current = c.s.invoke('collaboration', args, c.actor)
    assert result['message']['id'] == current['message']['id']
    assert [a['name'] for a in result['message']['attachments']] == ['first.txt', 'second.png']
    assert all(a['state'] == 'pending' for a in result['message']['attachments'])
    assert len(c.s.store.all('SELECT * FROM channel_attachments')) == 2
    assert len(c.s.store.all('SELECT * FROM weixin_deliveries')) == 1
    assert len(c.s.store.all('SELECT * FROM weixin_parts')) == 3  # text and EACH attachment
    assert c.s.store.one('SELECT state FROM collaboration_dot_messages WHERE message_id=?', (source,))['state'] == 'handled'
    receipt = c.s.task_liveness.receipt({**c.scope, 'dot_id': c.slot['id'], 'kind': 'message_v2',
                                       'request_key': args['idempotency_key']}, c.actor)
    assert receipt['committed'] and receipt['message_id'] == result['message']['id']
    assert not c.s.store.all('SELECT * FROM operations')
    with pytest.raises(DevError):
        c.s.invoke('collaboration', {**args, 'idempotency_key': key()}, c.actor)
    assert len(c.s.store.all('SELECT * FROM channel_attachments')) == 2


def test_failure_after_attachments_rolls_back_body_ack_shares_and_parts(collab, monkeypatch):
    c, w, account, binding, scope, source, args = approved(collab)
    original = c.s.task_liveness.record_receipt
    def fail(*values):
        raise RuntimeError('synthetic failure before outer transaction commit')
    monkeypatch.setattr(c.s.task_liveness, 'record_receipt', fail)
    with pytest.raises(RuntimeError):
        c.s.invoke('collaboration', args, c.actor)
    assert not c.s.store.all('SELECT * FROM channel_attachments')
    assert not c.s.store.all('SELECT * FROM weixin_parts')
    assert not c.s.store.all('SELECT * FROM weixin_deliveries')
    assert not c.s.store.all("SELECT id FROM collaboration_messages WHERE origin='bound_connector'")
    assert all(r['used_message_id'] is None for r in c.s.store.all('SELECT * FROM channel_attachment_approvals'))
    assert c.s.store.one('SELECT state FROM collaboration_dot_messages WHERE message_id=?', (source,))['state'] == 'pending'
    monkeypatch.setattr(c.s.task_liveness, 'record_receipt', original)
    assert c.s.invoke('collaboration', args, c.actor)['message_schema_version'] == 2


def test_share_cannot_be_used_for_other_source_or_fabricated_artifact(collab):
    c, w, account, binding, scope, source, args = approved(collab)
    other = ingest(c, w, account, packet(3))['message_ids'][0]
    with pytest.raises(DevError):
        c.s.invoke('collaboration', {**args, 'reply_to_id': other}, c.actor)
    with pytest.raises(DevError):
        c.s.invoke('collaboration', {**args, 'attachment_approval_ids': [artifact(c)]}, c.actor)
    with pytest.raises(DevError):
        w.media.approve({**scope, 'source_message_id': source, 'expected_source_version': 1,
            'artifact_ids': [artifact(c, project='otherproj')], 'confirm_file_egress': True, 'idempotency_key': key()}, c.owner)
    assert not c.s.store.all('SELECT * FROM channel_attachments')
    assert not c.s.store.all("SELECT id FROM collaboration_messages WHERE origin='bound_connector'")


def test_legacy_limit_unchanged_and_v2_does_not_accept_paths_or_unapproved_files(collab):
    c, w, account, binding, scope = fixture_channel(collab)
    source = ingest(c, w, account)['message_ids'][0]
    args = {**c.scope, 'dot_id': c.slot['id'], 'reply_to_id': source, 'body_text': 'x' * 5100,
            'idempotency_key': key()}
    with pytest.raises(DevError):
        c.s.invoke('collaboration', {'action': 'dot_message', **args}, c.actor)
    with pytest.raises(DevError):
        c.s.invoke('collaboration', {'action': 'dot_message_v2', **args, 'attachments': ['/private/file']}, c.actor)
    result = c.s.invoke('collaboration', {'action': 'dot_message_v2', **args}, c.actor)
    assert result['message']['body_text'] == args['body_text']
    assert result['message_schema_version'] == 2


def test_pause_blocks_cached_attachment_bytes_and_stale_publications(collab):
    c, w, account, binding, scope, source, args = approved(collab)
    result = c.s.invoke('collaboration', args, c.actor)
    identifier = result['message']['attachments'][0]['id']
    claim = w.media.claim(identifier)
    w.media.commit(claim, b'synthetic file only')
    assert w.media.content(identifier, c.actor, 'bytes=0-8')[0] == b'synthetic'
    current = w.binding(binding['binding_id'], c.owner, c.room)
    w.control({**scope, 'binding_id': current['id'], 'expected_version': current['version'],
               'action': 'pause', 'idempotency_key': key()}, c.owner)
    for actor in (c.owner, c.actor):
        with pytest.raises(DevError):
            w.media.content(identifier, actor)
    assert c.s.store.one('SELECT content FROM channel_attachments WHERE id=?', (identifier,))['content']


def test_misleading_filename_does_not_make_active_content_an_image(collab):
    c, w, account, binding, scope, source, args = approved(collab)
    kind, mime = w.media.content_type(b'<svg onload="alert(1)"></svg>')
    assert (kind, mime) == ('file', 'application/octet-stream')
    kind, mime = w.media.content_type(b'<html><script>bad()</script></html>')
    assert kind == 'file' and mime == 'application/octet-stream'
    assert 'private_source' not in json.dumps(w.listing(scope, c.owner))
