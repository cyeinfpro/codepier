"""UI invalidation keeps current access and rollback semantics, without content."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab


def context(fixture):
    service, owner, _, _, room, _, _, scope = fixture
    return service, owner, room, {**scope, 'conversation_id': room['id']}


def test_revision_is_persistent_and_rollback_does_not_publish(collab):
    service, owner, room, raw = context(collab)
    hints = service.relay_hints
    before = hints.snapshot(raw, owner)
    with pytest.raises(RuntimeError):
        with service.store.transaction():
            service.audit(room, owner.actor, 'fixture', room['id'])
            raise RuntimeError('rollback')
    assert hints.snapshot(raw, owner) == before
    with service.store.transaction():
        service.audit(room, owner.actor, 'fixture', room['id'])
    after = hints.snapshot(raw, owner)
    assert after['revision'] != before['revision']
    assert after['access'] == before['access']
    assert len(after['revision']) == 64


def test_connector_cannot_open_panel_hint_stream(collab):
    service, _, worker, _, _, _, _, scope = collab
    with pytest.raises(DevError) as error:
        service.relay_hints.snapshot(scope, worker)
    assert error.value.code == 'PANEL_SESSION_REQUIRED'


@pytest.mark.asyncio
async def test_hint_queue_coalesces_and_enforces_per_owner_limit(collab):
    service, owner, _, raw = context(collab)
    hints = service.relay_hints
    who = hints.snapshot(raw, owner)['owner']
    queues = [hints.subscribe(who) for _ in range(4)]
    try:
        with pytest.raises(DevError) as error:
            hints.subscribe(who)
        assert error.value.code == 'TOO_MANY_STREAMS'
        for _ in range(20):
            hints.wake()
        await asyncio.sleep(0)
        assert all(queue.qsize() == 1 for queue in queues)
    finally:
        for queue in queues:
            hints.unsubscribe(queue)
    assert not hints._subscribers
    assert not service.runtime.watchers


@pytest.mark.asyncio
async def test_stream_contains_only_revision_and_closes_after_access_change(collab):
    service, owner, room, raw = context(collab)
    hints = service.relay_hints
    initial = hints.snapshot(raw, owner)
    queue = hints.subscribe(initial['owner'])

    async def connected():
        return False

    request = SimpleNamespace(is_disconnected=connected)
    stream = hints.stream(request, raw, SimpleNamespace(panel=lambda _: owner), queue, initial)
    first = await anext(stream)
    data = json.loads(next(line[6:] for line in first.splitlines() if line.startswith('data: ')))
    assert data == {'revision': initial['revision']}
    assert room['id'] not in first and 'owner' not in first
    with service.store.transaction():
        service.store.execute("UPDATE collaboration_rooms SET version=version+1 WHERE id=?", (room['id'],))
    queue.put_nowait(None)
    assert await anext(stream) == 'event: access_changed\ndata: {}\n\n'
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert queue not in service.runtime.watchers


@pytest.mark.asyncio
async def test_auth_revocation_closes_stream_without_disclosing_state(collab):
    service, owner, _, raw = context(collab)
    hints = service.relay_hints
    initial = hints.snapshot(raw, owner)
    queue = hints.subscribe(initial['owner'])

    async def connected():
        return False

    def revoked(_):
        raise DevError('SESSION_EXPIRED', 'expired', 401)

    stream = hints.stream(SimpleNamespace(is_disconnected=connected), raw,
                          SimpleNamespace(panel=revoked), queue, initial)
    assert await anext(stream) == 'event: access_changed\ndata: {}\n\n'
    await stream.aclose()
    assert queue not in service.runtime.watchers
