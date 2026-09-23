"""Exercise real child failures and durable recovery without touching live agents."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from agent import symbol_worker
from agent.journal import Journal, MAX_INTERRUPTED_READS
from agent.symbols import analyze
from shared.contracts import TOOLS
from shared.util import DevError
from tests.test_reliability import local_agent


SOURCE = b'function target(){return dependency;}'


def worker_script(monkeypatch, script):
    monkeypatch.setattr(symbol_worker, '_worker_command',
                        lambda: [sys.executable, '-I', '-c', script])


@pytest.mark.parametrize('script', [
    'import os; os._exit(17)',
    pytest.param('import os,signal,resource; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); '
                 'os.kill(os.getpid(),signal.SIGSEGV)', marks=pytest.mark.skipif(os.name != 'posix', reason='POSIX signal')),
])
def test_native_worker_failure_is_caught_and_next_parse_succeeds(monkeypatch, script):
    with monkeypatch.context() as patch:
        worker_script(patch, script)
        with pytest.raises(DevError) as error:
            analyze('fixture.ts', SOURCE)
        assert error.value.code == 'PARSER_CRASHED'
    assert analyze('fixture.ts', SOURCE)['symbols'][0]['name'] == 'target'


def test_timeout_kills_and_reaps_worker_then_releases_slot(monkeypatch):
    created = []
    real_popen = subprocess.Popen
    def spawn(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        created.append(child)
        return child
    with monkeypatch.context() as patch:
        patch.setattr(symbol_worker.subprocess, 'Popen', spawn)
        worker_script(patch, 'import time; time.sleep(60)')
        started = time.monotonic()
        with pytest.raises(DevError) as error:
            analyze('fixture.ts', SOURCE, timeout=.15)
        assert error.value.code == 'PARSER_TIMEOUT'
        assert time.monotonic() - started < 3
    assert len(created) == 1 and created[0].returncode is not None
    assert analyze('fixture.py', b'def healthy(): pass')['symbols'][0]['name'] == 'healthy'


@pytest.mark.parametrize('response', ['not JSON', '{}', '[]', '{"ok":true,"data":null}',
                                    '{"ok":false,"error":{}}'])
def test_invalid_worker_responses_are_controlled(monkeypatch, response):
    worker_script(monkeypatch, f'print({response!r})')
    with pytest.raises(DevError) as error:
        analyze('fixture.ts', SOURCE)
    assert error.value.code == 'PARSER_FAILED'


def test_oversized_response_is_not_loaded_into_agent_memory(monkeypatch):
    worker_script(monkeypatch, 'import sys; sys.stdout.write("x" * 4096)')
    monkeypatch.setattr(symbol_worker, 'MAX_RESULT_BYTES', 1024)
    with pytest.raises(DevError) as error:
        analyze('fixture.ts', SOURCE)
    assert error.value.code == 'CODE_ANALYSIS_LIMIT'


def test_worker_start_failure_is_controlled(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('private local environment detail')
    monkeypatch.setattr(symbol_worker.subprocess, 'Popen', fail)
    with pytest.raises(DevError) as error:
        analyze('fixture.ts', SOURCE)
    assert error.value.code == 'PARSER_UNAVAILABLE'
    assert 'private' not in error.value.message


def test_parser_limits_and_unsupported_language_do_not_spawn(monkeypatch):
    def unexpected():
        pytest.fail('invalid request started a worker')
    monkeypatch.setattr(symbol_worker, '_worker_command', unexpected)
    for path, source, expected in [('fixture.ts', b'x' * (1024 * 1024 + 1), 'CODE_ANALYSIS_LIMIT'),
                                   ('fixture.txt', b'text', 'UNSUPPORTED_LANGUAGE')]:
        with pytest.raises(DevError) as error:
            analyze(path, source)
        assert error.value.code == expected


def test_parser_ignores_project_pythonpath_and_never_executes_source(tmp_path, monkeypatch):
    marker = tmp_path/'executed'
    (tmp_path/'tree_sitter.py').write_text('raise RuntimeError("project module imported")')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert analyze('fixture.ts', SOURCE)['symbols'][0]['name'] == 'target'
    source = f'from pathlib import Path\nPath({str(marker)!r}).write_text("must not execute")\ndef name(): pass\n'
    assert analyze('fixture.py', source.encode())['symbols'][0]['name'] == 'name'
    assert not marker.exists()


def test_parser_concurrency_and_admission_are_bounded(monkeypatch):
    # Hold exactly two real children at stdin. Do not assume eight interpreter
    # startups will all fit within the production two-second admission budget.
    created = []
    peak = 0
    lock = threading.Lock()
    ready = threading.Event()
    release = threading.Event()
    real_popen = subprocess.Popen

    def spawn(*args, **kwargs):
        nonlocal peak
        child = real_popen(*args, **kwargs)
        with lock:
            created.append(child)
            initial = len(created) <= 2
            peak = max(peak, sum(p.poll() is None for p in created))
            if len(created) == 2:
                ready.set()
        if initial and not release.wait(12):
            child.kill()
            child.wait()
            raise AssertionError('Controlled parser children were not released')
        return child

    worker_script(monkeypatch, 'import sys; sys.stdin.buffer.read(); print("{}")')
    monkeypatch.setattr(symbol_worker.subprocess, 'Popen', spawn)

    def check_failed():
        with pytest.raises(DevError) as error:
            analyze('fixture.ts', SOURCE)
        assert error.value.code == 'PARSER_FAILED'

    def check_busy():
        with pytest.raises(DevError) as error:
            analyze('fixture.ts', SOURCE, timeout=.02)
        assert error.value.code == 'PARSER_BUSY'

    with ThreadPoolExecutor(max_workers=8) as pool:
        running = [pool.submit(check_failed) for _ in range(2)]
        try:
            assert ready.wait(10), 'Two parser workers did not start'
            blocked = [pool.submit(check_busy) for _ in range(6)]
            for future in blocked:
                future.result(timeout=5)
            assert len(created) == 2 and peak == 2
        finally:
            release.set()
        for future in running:
            future.result(timeout=15)
        # Reclaimed slots admit subsequent batches; no rejected call was replayed.
        for _ in range(3):
            batch = [pool.submit(check_failed) for _ in range(2)]
            for future in batch:
                future.result(timeout=15)
    assert len(created) == 8 and peak == 2
    assert all(child.returncode is not None for child in created)
    with symbol_worker._SLOTS, symbol_worker._SLOTS:
        check_busy()
    assert len(created) == 8


@pytest.mark.asyncio
async def test_hung_parse_preserves_agent_heartbeat_and_receipt(local_agent, monkeypatch):
    agent, root = local_agent
    (root/'fixture.ts').write_bytes(SOURCE)
    packets = []
    async def send(packet):
        packets.append(packet)
        return True
    agent.send = send
    project = {'root': str(root), 'alias': 'fixture', 'mode': 'write'}
    request = {'id': 'parse', 'tool': 'code_symbols', 'project': project,
               'args': {'project': 'fixture', 'path': 'fixture.ts'}}
    with monkeypatch.context() as patch:
        worker_script(patch, 'import time; time.sleep(60)')
        patch.setattr(symbol_worker, 'TIMEOUT_SECONDS', .4)
        job = asyncio.create_task(agent.handle(request))
        await asyncio.sleep(.05)
        heartbeat = asyncio.create_task(agent.heartbeat())
        try:
            await asyncio.wait_for(agent.handle({'id': 'status', 'tool': 'agent_diagnostics',
                'project': project, 'args': {'project': 'fixture'}}), 1)
            assert not job.done()
            await asyncio.wait_for(job, 3)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        assert any(p['type'] == 'heartbeat' for p in packets)
        receipt = agent.journal.status('parse')['result']
        assert receipt['error']['code'] == 'PARSER_TIMEOUT'
        assert agent.journal.status('status')['result']['ok']
        assert not agent.active_roots
    # A duplicate delivery returns the saved failure, never executes again.
    await agent.handle(request)
    assert agent.journal.status('parse')['result'] == receipt
    await agent.handle({**request, 'id': 'healthy'})
    assert agent.journal.status('healthy')['result']['ok']


@pytest.mark.parametrize('mode', ['symbols', 'references'])
def test_search_reports_parser_crash_including_last_file(local_agent, monkeypatch, mode):
    agent, root = local_agent
    (root/'fixture.ts').write_bytes(SOURCE)
    project = {'id': 'p', 'root': str(root), 'alias': 'fixture', 'mode': 'write'}
    start = TOOLS['searches_start'].model.model_validate(
        {'project': 'fixture', 'query': 'target', 'mode': mode, 'file_glob': '*.ts'}).model_dump()
    search_id = 'a' * 32
    agent.searches.start(search_id, project, start)
    worker_script(monkeypatch, 'import os; os._exit(17)')
    query = TOOLS['searches_get'].model.model_validate({'project': 'fixture', 'search_id': search_id}).model_dump()
    result = agent.searches.get(project, query)
    assert result['state'] == 'failed' and result['error'] == 'PARSER_CRASHED'
    assert result['truncated'] and result['skipped_files'] == 1
    assert not result['has_more']
    assert agent.searches.get(project, query)['state'] == 'failed'


def test_repeated_interrupted_read_stops_and_receipt_survives_restart(tmp_path):
    directory = tmp_path/'state'
    request = {'tool': 'code_symbols', 'args': {'path': 'fixture.ts'}}
    journal = Journal(directory)
    identity = journal.journal_id
    for attempt in range(MAX_INTERRUPTED_READS):
        assert journal.start('poison', request) is None
        journal.mark_running('poison')
        journal.db.close()
        journal = Journal(directory)
        assert journal.journal_id == identity
        if attempt + 1 < MAX_INTERRUPTED_READS:
            assert journal.status('poison')['status'] == 'retryable'
    result = journal.start('poison', request)
    assert result['error']['code'] == 'AGENT_RESTART_LIMIT'
    assert journal.outbox() == [{'id': 'poison', 'result': result}]
    journal.db.close()
    journal = Journal(directory)
    try:
        assert journal.start('poison', request) == result
        assert journal.start('different-read', {'tool': 'fs_read'}) is None
        journal.ack('poison')
        assert journal.outbox() == []
    finally:
        journal.db.close()


def test_unstarted_requests_do_not_consume_restart_budget(tmp_path):
    directory = tmp_path/'state'
    journal = Journal(directory)
    try:
        for _ in range(MAX_INTERRUPTED_READS + 2):
            assert journal.start('waiting', {'tool': 'fs_read'}) is None
            journal.db.close()
            journal = Journal(directory)
        row = journal.db.execute("SELECT status,recovery_attempts FROM calls WHERE id='waiting'").fetchone()
        assert tuple(row) == ('retryable', 0)
    finally:
        journal.db.close()


def test_existing_journal_migrates_without_losing_receipts(tmp_path):
    directory = tmp_path/'state'
    directory.mkdir()
    db = sqlite3.connect(directory/'agent.sqlite3')
    db.execute('CREATE TABLE calls (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, '
               'result TEXT, acked INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL, tool TEXT)')
    result = {'ok': True, 'data': {'content': 'saved'}}
    db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?)', ('old', 'fingerprint', 'done', json.dumps(result), 0, 1, 'fs_read'))
    db.commit()
    db.close()
    journal = Journal(directory)
    try:
        assert journal.status('old')['result'] == result
        assert journal.db.execute("SELECT recovery_attempts FROM calls WHERE id='old'").fetchone()[0] == 0
    finally:
        journal.db.close()
