"""Durable live output stays separate from wide operation/history rows."""
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hub.runtime import Runtime
from hub.store import Store
from shared.util import DevError
from tests.test_reliability import Peer, runtime  # noqa: F401


async def start(runtime, principal=None):
    instance, owner = runtime
    receipt = await instance.invoke('shell_exec', {
        'project': 'ProjectAlpha', 'command': 'fixture command',
        'idempotency_key': 'output-storage-fixture'}, principal or owner)
    peer = Peer()
    instance.connections['dev'] = peer
    return instance, principal or owner, peer, receipt['operation_id']


def emit(instance, peer, identifier, text, sequence=1, *, snapshot=True):
    return instance._receive_operation_event('dev', peer, {
        'type': 'output', 'id': identifier, 'text': text,
        'seq': sequence, 'snapshot': snapshot})


def finish(instance, identifier, **data):
    instance.complete({'id': identifier}, {'ok': True, 'data': {'exit_code': 0, **data}})


@pytest.mark.asyncio
async def test_live_tail_is_durable_and_does_not_rewrite_wide_operation_body(runtime):
    instance, principal, peer, identifier = await start(runtime)
    text = '多字节 output ' * 16000
    emit(instance, peer, identifier, text, 8)
    retained = text[-131072:]
    raw = instance.store.one('SELECT output,output_seq FROM operations WHERE id=?', (identifier,))
    assert raw == {'output': '', 'output_seq': 8}
    assert instance.store.one('SELECT output FROM operation_output_tails WHERE operation_id=?',
                              (identifier,))['output'] == retained
    public = instance.operation(identifier, principal)
    assert public['output'] == retained and public['output_chars'] == len(retained)
    assert public['last_output_at'] is not None
    # A second independent connection observes the committed snapshot, not an
    # in-process buffer that could disappear when the Hub process dies.
    with sqlite3.connect(instance.store.state_directory / 'hub.sqlite3') as observer:
        assert observer.execute('SELECT output FROM operation_output_tails WHERE operation_id=?',
                                (identifier,)).fetchone()[0] == retained
    reopened = Store(instance.store.directory)
    try:
        recovered = Runtime(reopened).operation(identifier, principal)
        assert recovered['output'] == retained and recovered['output_seq'] == 8
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stale', [1, 2, 3, True, None, '9'])
async def test_replayed_or_invalid_snapshot_does_not_move_cursor_or_replace_tail(runtime, stale):
    instance, principal, peer, identifier = await start(runtime)
    emit(instance, peer, identifier, 'authoritative tail', 3)
    before = instance.operation(identifier, principal)
    emit(instance, peer, identifier, 'stale tail', stale)
    after = instance.operation(identifier, principal)
    for field in ('output', 'output_seq', 'last_output_at', 'updated'):
        assert after[field] == before[field]


@pytest.mark.asyncio
async def test_legacy_append_and_snapshot_jumps_preserve_bounded_tail(runtime):
    instance, principal, peer, identifier = await start(runtime)
    instance.store.execute('UPDATE operations SET output=?,output_seq=2 WHERE id=?',
                           ('legacy history ', identifier))
    emit(instance, peer, identifier, 'first append', snapshot=False)
    assert instance.operation(identifier, principal)['output'] == 'legacy history first append'
    emit(instance, peer, identifier, 'replacement snapshot', 12)
    emit(instance, peer, identifier, 'x' * 9000, snapshot=False)
    current = instance.operation(identifier, principal)
    assert current['output'] == 'replacement snapshot' + 'x' * 8192
    assert current['output_seq'] == 13
    assert instance.store.one('SELECT output FROM operations WHERE id=?', (identifier,))['output'] == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('final_output', [None, '', 'final result tail'])
async def test_terminal_commit_materializes_tail_and_rejects_late_output(runtime, final_output):
    instance, principal, peer, identifier = await start(runtime)
    emit(instance, peer, identifier, 'streamed tail', 7)
    finish(instance, identifier, **({} if final_output is None else {'output': final_output}))
    expected = 'streamed tail' if final_output is None else final_output
    final = instance.operation(identifier, principal)
    assert final['state'] == 'succeeded' and final['output'] == expected
    assert final['output_seq'] == 7 + int(expected != 'streamed tail')
    assert instance.store.one('SELECT output FROM operations WHERE id=?', (identifier,))['output'] == expected
    assert not instance.store.all('SELECT * FROM operation_output_tails')
    emit(instance, peer, identifier, 'late high-sequence output', 1000)
    assert instance.operation(identifier, principal)['output'] == expected
    assert instance.operation(identifier, principal)['output_seq'] == final['output_seq']
    assert not instance.store.all('SELECT * FROM operation_output_tails')


@pytest.mark.asyncio
async def test_failed_completion_outbox_rolls_back_final_tail_and_sequence(runtime, monkeypatch):
    instance, principal, peer, identifier = await start(runtime)
    emit(instance, peer, identifier, 'durable live tail', 4)

    def fail(_):
        raise sqlite3.OperationalError('fixture outbox unavailable')

    monkeypatch.setattr(instance.collaboration, 'events', SimpleNamespace(operation_completed=fail))
    with pytest.raises(sqlite3.OperationalError, match='fixture outbox'):
        finish(instance, identifier, output='terminal tail')
    current = instance.operation(identifier, principal)
    assert current['pending'] and current['result'] is None
    assert current['output'] == 'durable live tail' and current['output_seq'] == 4
    assert instance.store.one('SELECT output FROM operations WHERE id=?', (identifier,))['output'] == ''
    committed = []
    monkeypatch.setattr(instance.collaboration, 'events', SimpleNamespace(operation_completed=committed.append))
    finish(instance, identifier, output='terminal tail')
    assert committed == [identifier]
    assert instance.operation(identifier, principal)['output_seq'] == 5
    assert not instance.store.all('SELECT * FROM operation_output_tails')


@pytest.mark.asyncio
async def test_compact_read_uses_length_and_retains_original_output_cursor(runtime, monkeypatch):
    instance, principal, peer, identifier = await start(runtime)
    emit(instance, peer, identifier, 'fixture output' * 9000, 9)
    original, queries = instance.store.one, []

    def observe(sql, *args):
        queries.append(sql)
        return original(sql, *args)

    monkeypatch.setattr(instance.store, 'one', observe)
    compact = instance.operation(identifier, principal, {'include_output': False, 'include_result': False})
    assert compact['output_chars'] == len('fixture output' * 9000)
    assert 'output' not in compact and 'after_output_seq' not in compact['next_call']['arguments']
    tail_queries = [sql for sql in queries if 'FROM operation_output_tails' in sql]
    assert tail_queries and all('length(output)' in sql for sql in tail_queries)
    delivered = instance.operation(identifier, principal, {'output_limit': 5})
    assert delivered['output'] == 'utput' and delivered['output_truncated']
    assert delivered['next_call']['arguments']['after_output_seq'] == 9


@pytest.mark.asyncio
async def test_reauthorization_precedes_tail_read(runtime, monkeypatch):
    from tests.legacy_iam_fixture import seed_grant
    instance, owner = runtime
    seed_grant(instance.store, 'output-reader', owner.user_id, projects=('proj',))
    principal = replace(owner, admin=False, actor='mcp:output-reader:fixture', grant_id='output-reader')
    instance, principal, peer, identifier = await start(runtime, principal)
    emit(instance, peer, identifier, 'private live output', 1)
    instance.store.execute("UPDATE grants SET revoked=1 WHERE id='output-reader'")
    original, tails = instance.store.one, []

    def observe(sql, *args):
        if 'FROM operation_output_tails' in sql:
            tails.append(sql)
        return original(sql, *args)

    monkeypatch.setattr(instance.store, 'one', observe)
    with pytest.raises(DevError):
        instance.operation(identifier, principal)
    assert not tails


@pytest.mark.asyncio
async def test_output_inbound_projection_does_not_load_result_payload_or_old_tail(runtime, monkeypatch):
    instance, _, peer, identifier = await start(runtime)
    original, queries = instance.store.one, []

    def observe(sql, *args):
        if 'FROM operations' in sql:
            queries.append(sql)
        return original(sql, *args)

    monkeypatch.setattr(instance.store, 'one', observe)
    emit(instance, peer, identifier, 'small snapshot', 1)
    assert queries
    assert all('*' not in sql.split('FROM')[0] and 'payload' not in sql.split('FROM')[0]
               and 'result' not in sql.split('FROM')[0] for sql in queries)


def test_additive_migration_does_not_rewrite_history_or_credentials(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        store.execute("INSERT INTO operations(id,actor,tool,args_summary,fingerprint,state,output,created,updated) "
                      "VALUES('history','fixture','exec','{}','','succeeded',?,1,1)", ('historical output',))
        cipher = store.encrypt('fixture credential')
        key = (store.state_directory / 'master.key').read_bytes()
        store.execute("DROP TABLE operation_output_tails")
    finally:
        store.close()
    upgraded = Store(tmp_path / 'hub')
    try:
        assert upgraded.one("SELECT output FROM operations WHERE id='history'")['output'] == 'historical output'
        assert not upgraded.all('SELECT * FROM operation_output_tails')
        assert upgraded.decrypt(cipher) == 'fixture credential'
        assert (upgraded.state_directory / 'master.key').read_bytes() == key
        assert upgraded.one("SELECT value FROM meta WHERE key='schema'")['value'] == '10'
        assert not upgraded.all('PRAGMA foreign_key_check')
        assert 'fixture credential' not in json.dumps(upgraded.stats_snapshot())
    finally:
        upgraded.close()
