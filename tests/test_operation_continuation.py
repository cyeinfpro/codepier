"""Long command waits are bounded reads; receipts survive absent/cancelled readers."""
import asyncio
from dataclasses import replace
import time

import jsonschema
import pytest

from shared.contracts import OUTPUT_SCHEMAS, TOOLS
from shared.util import DevError
from tests.test_reliability import runtime


async def submit(r, p):
    return await r.invoke('shell_exec', {'project': 'Imago', 'command': 'pytest -q',
                                       'idempotency_key': 'one-intentional-test'}, p)


def finish(r, identifier, *, output='test result', exit_code=0):
    r.complete(r.store.one('SELECT * FROM operations WHERE id=?', (identifier,)),
               {'ok': True, 'data': {'exit_code': exit_code, 'command_ok': exit_code == 0,
                                     'output': output, 'timed_out': False, 'cancelled': False}})


async def registered(r, identifier, count):
    async with asyncio.timeout(1):
        while len(r.operation_waiters.get(identifier, ())) != count:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_receipt_guides_bounded_deduplicated_reads_until_completion(runtime):
    r, p = runtime
    receipt = await submit(r, p)
    identifier = receipt['operation_id']
    following = receipt['next_call']
    assert following['name'] == 'operations_wait'
    TOOLS[following['name']].model.model_validate(following['arguments'])
    assert following['arguments']['operation_id'] == identifier
    assert 'after_output_seq' not in following['arguments']
    jsonschema.validate(receipt, OUTPUT_SCHEMAS['shell_exec'])
    # Each status read is bounded even when a test emits a large log.
    output = 'x' * 12000
    r.store.execute('UPDATE operations SET output=?,output_seq=3 WHERE id=?', (output, identifier))
    first = await r.invoke(following['name'], {**following['arguments'], 'wait_seconds': 0}, p)
    assert first['output'] == output[-8000:] and first['output_truncated']
    assert first['next_call']['arguments']['after_output_seq'] == 3
    again = await r.invoke('operations_wait', {**first['next_call']['arguments'], 'wait_seconds': 0}, p)
    assert 'output' not in again and again['output_unchanged']
    assert again['next_call'] == first['next_call']
    assert again['pending'] and again['elapsed_seconds'] >= 0
    finish(r, identifier, output=output, exit_code=1)
    ended = await r.invoke(again['next_call']['name'], again['next_call']['arguments'], p)
    assert ended['state'] == 'failed' and not ended['pending']
    assert ended['result']['data']['exit_code'] == 1 and ended['next_call'] is None
    jsonschema.validate(ended, OUTPUT_SCHEMAS['operations_wait'])
    assert r.store.one('SELECT count(*) n FROM operations')['n'] == 1
    # A full explicit read still retrieves the retained log.
    detailed = await r.invoke('operations_get', {'operation_id': identifier}, p)
    assert detailed['output'] == output and not detailed['output_truncated']


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [dict(include_output=False), dict(output_limit=0),
                                     dict(include_output=False, after_output_seq=1)])
async def test_status_only_reads_do_not_advance_past_undelivered_logs(runtime, options):
    r, p = runtime
    identifier = (await submit(r, p))['operation_id']
    r.store.execute("UPDATE operations SET output='new log',output_seq=2 WHERE id=?", (identifier,))
    status = await r.invoke('operations_wait', {'operation_id': identifier, 'wait_seconds': 0, **options}, p)
    following = status['next_call']['arguments']
    assert following.get('after_output_seq') == options.get('after_output_seq')
    delivered = await r.invoke('operations_wait', {**following, 'wait_seconds': 0}, p)
    assert delivered['output'] == 'new log'


@pytest.mark.asyncio
async def test_compact_terminal_read_retrieves_result_once_with_final_output(runtime):
    r, p = runtime
    identifier = (await submit(r, p))['operation_id']
    finish(r, identifier, output='FAILED: useful diagnostic', exit_code=1)
    compact = await r.invoke('operations_wait', {'operation_id': identifier, 'include_result': False,
                                                'include_output': False, 'after_output_seq': 1}, p)
    assert not compact['pending'] and compact['result_omitted']
    following = compact['next_call']
    assert following['name'] == 'operations_get'
    assert 'after_output_seq' not in following['arguments']
    recovered = await r.invoke(following['name'], following['arguments'], p)
    assert recovered['next_call'] is None and recovered['result']['data']['exit_code'] == 1
    assert 'useful diagnostic' in recovered['output']
    assert 'useful diagnostic' in recovered['result']['data']['output']
    # Terminal elapsed time is the saved duration, not time since the last query.
    assert recovered['elapsed_seconds'] == compact['elapsed_seconds']


@pytest.mark.asyncio
async def test_wait_expiry_performs_two_database_reads_and_keeps_command(runtime, monkeypatch):
    r, p = runtime
    identifier = (await submit(r, p))['operation_id']
    original, reads = r.store.one, []
    def counted(sql, *args):
        if 'FROM operations' in sql:
            reads.append(sql)
        return original(sql, *args)
    monkeypatch.setattr(r.store, 'one', counted)
    status = await r.invoke('operations_wait', {'operation_id': identifier, 'wait_seconds': 1}, p)
    assert status['pending'] and status['next_call']['name'] == 'operations_wait'
    assert len(reads) == 2 and '*' not in reads[0]
    assert not r.operation_waiters
    assert original('SELECT cancel_requested FROM operations WHERE id=?', (identifier,))['cancel_requested'] == 0


@pytest.mark.asyncio
async def test_cancelling_one_reader_does_not_cancel_other_reader_or_command(runtime):
    r, p = runtime
    identifier = (await submit(r, p))['operation_id']
    args = {'operation_id': identifier, 'wait_seconds': 10}
    readers = [asyncio.create_task(r.invoke('operations_wait', args, p)) for _ in range(2)]
    try:
        await registered(r, identifier, 2)
        readers[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await readers[0]
        assert not readers[1].done()
        assert r.operation(identifier, p)['cancel_requested'] == 0
        finish(r, identifier)
        status = await asyncio.wait_for(readers[1], 1)
        assert status['state'] == 'succeeded' and status['next_call'] is None
        assert not r.operation_waiters
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


@pytest.mark.asyncio
async def test_completion_between_subscription_and_wait_is_not_lost(runtime, monkeypatch):
    r, p = runtime
    identifier = (await submit(r, p))['operation_id']
    original = r.operation_row
    def completing(*args, **kwargs):
        row = original(*args, **kwargs)
        if kwargs.get('status_only'):
            finish(r, identifier)
        return row
    monkeypatch.setattr(r, 'operation_row', completing)
    result = await asyncio.wait_for(r.invoke('operations_wait', {'operation_id': identifier}, p), 1)
    assert result['state'] == 'succeeded' and not r.operation_waiters


@pytest.mark.asyncio
async def test_shutdown_wakes_readers_without_cancelling_command(runtime):
    r, p = runtime
    identifier = (await submit(r, p))['operation_id']
    reader = asyncio.create_task(r.invoke('operations_wait', {'operation_id': identifier}, p))
    try:
        await registered(r, identifier, 1)
        await r.stop()
        status = await asyncio.wait_for(reader, 1)
        assert status['pending'] and status['cancel_requested'] == 0
        assert status['next_call']['arguments']['operation_id'] == identifier
        assert not r.operation_waiters
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_wait_rechecks_receipt_authorization_before_exposing_result(runtime):
    r, admin = runtime
    p = replace(admin, admin=False, actor='client', grant_id=None)
    identifier = (await submit(r, p))['operation_id']
    reader = asyncio.create_task(r.invoke('operations_wait', {'operation_id': identifier}, p))
    try:
        await registered(r, identifier, 1)
        r.store.execute("UPDATE operations SET grant_id='different-grant' WHERE id=?", (identifier,))
        finish(r, identifier, output='private result')
        with pytest.raises(DevError, match='找不到此授权范围内的操作'):
            await asyncio.wait_for(reader, 1)
        assert not r.operation_waiters
        with pytest.raises(DevError):
            await r.invoke('operations_wait', {'operation_id': identifier}, p)
        assert not r.operation_waiters
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiter_does_not_retain_completed_payload_and_audit_failure_cannot_hide_completion(runtime, monkeypatch):
    r, p = runtime
    identifier = (await submit(r, p))['operation_id']
    reader = asyncio.create_task(r.wait_operation(identifier, p, 10))
    try:
        await registered(r, identifier, 1)
        def broken_audit(*args, **kwargs):
            raise OSError('audit unavailable')
        monkeypatch.setattr(r.store, 'audit', broken_audit)
        with pytest.raises(OSError):
            finish(r, identifier, output='x' * 10000)
        await asyncio.wait_for(reader, 1)
        assert not r.operation_waiters
        result = r.operation(identifier, p)
        assert result['state'] == 'succeeded' and result['next_call'] is None
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['failed', 'cancelled', 'needs_review', 'interrupted'])
async def test_terminal_receipt_without_result_does_not_instruct_endless_fetch(runtime, state):
    r, p = runtime
    identifier = (await submit(r, p))['operation_id']
    r.store.execute('UPDATE operations SET state=?,result=NULL,updated=? WHERE id=?', (state, time.time(), identifier))
    status = await asyncio.wait_for(r.invoke('operations_wait', {'operation_id': identifier, 'include_result': False}, p), 1)
    assert not status['pending'] and status['next_call'] is None
    assert not r.operation_waiters
