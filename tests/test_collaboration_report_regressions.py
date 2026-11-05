"""Synthetic regressions for collaboration audit failure boundaries."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from hub.collaboration.telegram import TelegramService
from hub.collaboration.weixin import WeixinService
from shared.util import DevError


@pytest.mark.parametrize('service,code,status,expected', [
    (TelegramService, 'ROOM_PAUSED', 409, 'active'),
    (TelegramService, 'CHANNEL_MAPPING_CHANGED', 403, 'active'),
    (TelegramService, 'TELEGRAM_BINDING_CHANGED', 409, 'active'),
    (TelegramService, 'TELEGRAM_PROVIDER_REJECTED', 403, 'active'),
    (TelegramService, 'TELEGRAM_PROVIDER_REJECTED', 401, 'attention'),
    (WeixinService, 'ROOM_PAUSED', 409, 'active'),
    (WeixinService, 'WEIXIN_MEMBER_REMOVED', 403, 'active'),
    (WeixinService, 'WEIXIN_DUPLICATE_CONFLICT', 409, 'active'),
    (WeixinService, 'WEIXIN_AUTH_REJECTED', 403, 'attention'),
    (WeixinService, 'WEIXIN_SESSION_EXPIRED', 403, 'attention'),
])
def test_poll_local_failures_do_not_revoke_account(service, code, status, expected):
    row = dict(id='synthetic', poll_fence=2, poll_owner='owner', state='active', failures=0)
    writes = []
    store = SimpleNamespace(transaction=nullcontext, one=lambda *a: row,
                            execute=lambda sql, args: writes.append((sql, args)))
    transport = object.__new__(service)
    transport.store = store
    transport.c = SimpleNamespace(clock=lambda: 100)
    transport.poll_failed(row.copy(), DevError(code, 'synthetic', status))
    assert writes[-1][1][0] == expected
    assert 'poll_until=0' in writes[-1][0]


@pytest.mark.parametrize('service', [TelegramService, WeixinService])
def test_stale_poll_failure_cannot_overwrite_replacement(service):
    row = dict(id='synthetic', poll_fence=3, poll_owner='owner', state='active', failures=0)
    writes = []
    transport = object.__new__(service)
    transport.store = SimpleNamespace(transaction=nullcontext, one=lambda *a: row,
                                      execute=lambda *a: writes.append(a))
    transport.c = SimpleNamespace(clock=lambda: 100)
    transport.poll_failed({**row, 'poll_fence': 2}, DevError('ROOM_PAUSED', 'synthetic', 409))
    assert not writes


from tests.collaboration_support import collab as collab, key


def test_room_mutations_scope_shared_grant_by_member(collab):
    service, _, worker, *_ = collab
    request = {'idempotency_key': 'same-client-key'}
    for member in ('member-a', 'member-b'):
        args = {**request, 'member_id': member}
        result = service.rooms.mutation(worker, 'room_inbox_ack', args, lambda: {'member': member})
        assert result == {'member': member}
    with pytest.raises(DevError):
        service.rooms.mutation(worker, 'room_inbox_ack', {**request, 'member_id': 'member-a', 'changed': True}, lambda: {})


def test_room_pause_blocks_legacy_partition_admission(collab):
    service, owner, _, _, partition, *_ = collab
    room = service.rooms.create({'title': 'Pause regression', 'projects': [{'project': 'proj'}],
                                 'idempotency_key': key()}, owner)['room']
    service.rooms.update({'conversation_id': room['id'], 'expected_version': room['version'],
                          'state': 'paused', 'idempotency_key': key()}, owner)
    with pytest.raises(DevError) as stopped:
        service.live_room(partition)
    assert stopped.value.code == 'ROOM_PAUSED'
