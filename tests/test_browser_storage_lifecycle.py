"""Synthetic extension RPCs and local storage; no browser is contacted."""
import asyncio
import threading

import pytest

from shared.util import DevError
from tests.test_browser_close_recovery import browser_receipt as browser_receipt


def enable(broker):
    broker.agent.config = {'integrations': {'browser': {
        'enabled': True, 'projects': ['*'], 'origins': ['https://example.test'], 'lease_seconds': 60,
    }}}


@pytest.mark.asyncio
async def test_slow_status_keeps_loop_responsive_and_settles_repeated_cancellation(browser_receipt):
    broker, records, project, _ = browser_receipt
    entered, release = threading.Event(), threading.Event()
    original = records.list_page
    loop_thread = threading.get_ident()
    workers = []

    def slow(*args, **kwargs):
        workers.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    records.list_page = slow
    task = asyncio.create_task(broker.execute('status', 'browser_status', project, {}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert workers == [workers[0]] and workers[0] != loop_thread
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_project_close_waits_for_open_and_cannot_be_overwritten(browser_receipt):
    broker, records, project, existing = browser_receipt
    enable(broker)
    previous = records.load('browser', existing, project)
    previous.update(state='closed', tab_cleanup_confirmed=True)
    records.save('browser', existing, project, previous, replace=True)
    identifier = 'b' * 32
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def rpc(action, body, **kwargs):
        calls.append(action)
        if action == 'open':
            entered.set()
            await release.wait()
            return {'opened': True}
        return {'tab_cleanup_confirmed': True}

    broker.rpc = rpc
    opening = asyncio.create_task(broker.execute(identifier, 'browser_open', project,
                                  {'url': 'https://example.test/page', 'project': 'fixture'}))
    await asyncio.wait_for(entered.wait(), 2)
    closing = asyncio.create_task(broker.release_project(project))
    try:
        await asyncio.sleep(.03)
        assert calls == ['open']
    finally:
        release.set()
        await opening
        result = await closing
    assert calls == ['open', 'close']
    assert result[0]['tab_cleanup_confirmed']
    assert records.load('browser', identifier, project)['state'] == 'closed'


@pytest.mark.asyncio
async def test_concurrent_opens_keep_the_eight_lease_limit(browser_receipt):
    broker, records, project, _ = browser_receipt
    enable(broker)

    async def rpc(action, body, **kwargs):
        await asyncio.sleep(0)
        return {'opened': True}

    broker.rpc = rpc
    results = await asyncio.gather(*(broker.execute(f'{number:032x}', 'browser_open', project,
        {'url': 'https://example.test/page', 'project': 'fixture'}) for number in range(8)),
        return_exceptions=True)
    errors = [value for value in results if isinstance(value, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], DevError)
    assert errors[0].code == 'BROWSER_LEASE_LIMIT'
    assert len(records.list_page('browser', project)['records']) == 8
