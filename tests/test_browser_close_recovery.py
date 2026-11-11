"""Durable browser cleanup receipts; no real browser or provider is contacted."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from agent.background_browser import BrowserBroker
from agent.integration_state import Records
from shared.util import DevError


@pytest.fixture
def browser_receipt():
    database = sqlite3.connect(':memory:', check_same_thread=False)
    database.row_factory = sqlite3.Row
    records = Records(SimpleNamespace(db=database, lock=threading.RLock()))
    project = {'id': 'fixture-project', 'root': '/fixture/project',
               '_coding_device': 'fixture-device', '_integration_owner': 'fixture-owner'}
    identifier = 'a' * 32
    records.save('browser', identifier, project,
                 {'lease_id': identifier, 'state': 'ready', 'expires': time.time() + 60,
                  'observation_id': 'fixture-observation', 'observation_token': 'fixture-token'})
    broker = BrowserBroker(SimpleNamespace(config={}, engine=SimpleNamespace(root=lambda value: None)), records)
    try:
        yield broker, records, project, identifier
    finally:
        database.close()


@pytest.mark.parametrize('outcome', ['disconnected', 'unconfirmed', 'confirmed'])
def test_explicit_close_persists_cleanup_outcome_and_project_stop_recovers(browser_receipt, outcome):
    broker, records, project, identifier = browser_receipt
    calls = []

    async def close(action, body, **kwargs):
        assert action == 'close'
        calls.append(body['lease_id'])
        if len(calls) == 1 and outcome == 'disconnected':
            raise DevError('BROWSER_DISCONNECTED', 'fixture unavailable', 409)
        return {'tab_cleanup_confirmed': len(calls) > 1 or outcome == 'confirmed'}

    broker.rpc = close
    result = asyncio.run(broker.execute('b' * 32, 'browser_close', project, {'lease_id': identifier}))
    confirmed = outcome == 'confirmed'
    assert result['tab_cleanup_confirmed'] is confirmed
    saved = records.load('browser', identifier, project)
    assert saved['state'] == 'closed'
    assert saved['observation_id'] is None and saved['observation_token'] is None
    assert saved['tab_cleanup_confirmed'] is confirmed

    recovered = asyncio.run(broker.release_project(project))
    assert len(recovered) == (0 if confirmed else 1)
    assert calls == [identifier] * (1 if confirmed else 2)
    assert records.load('browser', identifier, project)['tab_cleanup_confirmed'] is True
    assert asyncio.run(broker.release_project(project)) == []


def test_close_cancellation_preserves_unconfirmed_cleanup_for_project_stop(browser_receipt):
    broker, records, project, identifier = browser_receipt

    async def cancelled(action, body, **kwargs):
        raise asyncio.CancelledError()

    broker.rpc = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(broker.execute('b' * 32, 'browser_close', project, {'lease_id': identifier}))
    saved = records.load('browser', identifier, project)
    assert saved['state'] == 'closed'
    assert saved['tab_cleanup_confirmed'] is False

    async def confirmed(action, body, **kwargs):
        return {'tab_cleanup_confirmed': True}

    broker.rpc = confirmed
    recovered = asyncio.run(broker.release_project(project))
    assert len(recovered) == 1 and recovered[0]['tab_cleanup_confirmed'] is True


@pytest.mark.asyncio
async def test_three_overlapping_closes_keep_one_lease_lock(browser_receipt):
    broker, records, project, identifier = browser_receipt
    entered = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(3)]
    calls = 0

    async def rpc(action, body, **kwargs):
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        await release[index].wait()
        return {'tab_cleanup_confirmed': True}

    broker.rpc = rpc

    async def close(started):
        started.set()
        return await broker.execute('b' * 32, 'browser_close', project, {'lease_id': identifier})

    tasks = []
    try:
        tasks.append(asyncio.create_task(close(asyncio.Event())))
        await asyncio.wait_for(entered[0].wait(), 1)
        second_started = asyncio.Event()
        tasks.append(asyncio.create_task(close(second_started)))
        await asyncio.wait_for(second_started.wait(), 1)
        release[0].set()
        await asyncio.wait_for(entered[1].wait(), 1)
        third_started = asyncio.Event()
        tasks.append(asyncio.create_task(close(third_started)))
        await asyncio.wait_for(third_started.wait(), 1)
        assert not entered[2].is_set(), 'closing one waiter must not replace the lock held by the next'
        release[1].set()
        await asyncio.wait_for(entered[2].wait(), 1)
        release[2].set()
        results = await asyncio.gather(*tasks)
        assert all(row['tab_cleanup_confirmed'] for row in results)
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not broker.locks
    assert records.load('browser', identifier, project)['tab_cleanup_confirmed'] is True


def test_project_cleanup_does_not_retain_locks_for_finished_receipts(browser_receipt):
    broker, records, project, identifier = browser_receipt
    records.save('browser', identifier, project,
                 {'lease_id': identifier, 'state': 'closed', 'tab_cleanup_confirmed': True},
                 replace=True)
    assert asyncio.run(broker.release_project(project)) == []
    assert not broker.locks
