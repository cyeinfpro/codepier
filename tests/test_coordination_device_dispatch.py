"""Coordination shares the per-device dispatch fence without retaining authority."""
import asyncio

import pytest

from shared.util import DevError
from tests.collaboration_support import collab  # noqa: F401
from tests.test_collaboration_coordination import setup_goal, make_work, claim, lease


def request(collab):
    c, service, owner, worker, broad, goal, scope, _ = setup_goal(collab)
    item = claim(c, scope, goal, make_work(c, scope, goal, worker), broad)
    args = {**lease(scope, goal, item), 'tool': 'exec',
            'arguments': {'command': 'printf isolated-fixture', 'timeout_seconds': 10}}
    return c, service, broad, args


@pytest.mark.asyncio
async def test_coordination_does_not_wait_for_unrelated_device(collab):
    c, service, principal, args = request(collab)
    async with service.runtime.device_dispatch_lock('unrelated-device'):
        receipt = await asyncio.wait_for(c.execute(args, principal), 3)
    assert receipt['pending']
    assert (await c.execute(args, principal))['operation_id'] == receipt['operation_id']
    assert service.store.one('SELECT count(*) n FROM operations')['n'] == 1


@pytest.mark.asyncio
async def test_coordination_waits_for_its_device_and_rechecks_revocation(collab, monkeypatch):
    c, service, principal, args = request(collab)
    device = c._dispatch_device(args, principal)
    lock = service.runtime.device_dispatch_lock(device)
    entered = asyncio.Event()
    original = service.runtime.device_dispatch_lock
    def selected(identifier):
        assert identifier == device
        entered.set()
        return original(identifier)
    monkeypatch.setattr(service.runtime, 'device_dispatch_lock', selected)
    async with lock:
        task = asyncio.create_task(c.execute(args, principal))
        await asyncio.wait_for(entered.wait(), 3)
        assert not task.done()
        assert service.store.one('SELECT count(*) n FROM operations')['n'] == 0
        service.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (principal.grant_id,))
    with pytest.raises(DevError):
        await task
    assert service.store.one('SELECT count(*) n FROM operations')['n'] == 0


def test_coordination_device_mapping_fence_precedes_admission(collab):
    c, service, principal, args = request(collab)
    with pytest.raises(DevError) as caught:
        c.admit(args, principal, expected_device_id='different-device')
    assert caught.value.code == 'PROJECT_MAPPING_CHANGED'
    assert service.store.one('SELECT count(*) n FROM operations')['n'] == 0
    assert service.store.one('SELECT steps FROM coordination_goals WHERE id=?', (args['goal_id'],))['steps'] == 0
