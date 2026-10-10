"""Pure checks for the opt-in loopback benchmark; no Hub/Agent is started."""
import json
import sqlite3
import time

import httpx
import pytest

from scripts import benchmark_hub_agent as benchmark


def test_percentiles_and_errors_are_not_hidden():
    assert benchmark.percentiles([1,2,3,4,100]) == {'p50':3,'p95':100,'p99':100}
    samples = [{'worker':0,'ok':True,'latency_ms':10,'polls':1},
               {'worker':1,'ok':False,'latency_ms':100,'polls':0}]
    summary = benchmark.summarize(samples,2)
    assert summary['error_rate']==.5 and summary['throughput_success_per_second']==.5
    assert summary['latency_ms_all']['p95']==100
    assert summary['queue_sample_count']==0 and summary['agent_queue_ms']['p95'] is None
    assert 0<summary['jain_inverse_worker_mean_latency']<1


@pytest.mark.asyncio
async def test_pending_poll_uses_only_original_id(monkeypatch):
    seen = []
    async def rpc(client,name,args):
        seen.append((name,args))
        return {'operations':[{'pending':False,'state':'succeeded','result':{'ok':True,'data':{'sha256':'a'*64}}}]},False
    monkeypatch.setattr(benchmark,'rpc',rpc)
    identifier,data,failed,polls,state = await benchmark.finish_original(
        None,{'pending':True,'operation_id':'original'},False,time.monotonic()+1)
    assert identifier=='original' and not failed and polls==1 and state=='succeeded'
    assert data['sha256']=='a'*64
    assert seen[0][0]=='task_query'
    assert seen[0][1]['operation_ids']==['original']


def test_extract_text_fallback_and_error():
    response = httpx.Response(200,request=httpx.Request('POST','http://127.0.0.1/mcp'),
        json={'result':{'content':[{'type':'text','text':json.dumps({'error':{'code':'DENIED'}})}],'isError':True}})
    assert benchmark.extract(response)==({'error':{'code':'DENIED'}},True)


@pytest.mark.parametrize('attempts', [1, 2])
def test_enrich_only_relative_agent_and_hub_timings(tmp_path, attempts):
    database = tmp_path/'fixture.sqlite3'
    with sqlite3.connect(database) as db:
        db.executescript('''
            CREATE TABLE operations(id TEXT,created REAL,accepted_at REAL,updated REAL,state TEXT,attempts INT);
            CREATE TABLE operation_events(id INTEGER PRIMARY KEY,operation_id TEXT,source TEXT,seq INT,stage TEXT,at REAL,elapsed_ms INT);
            INSERT INTO operations VALUES('original',100,101,105,'succeeded',1);
            INSERT INTO operation_events VALUES(1,'original','hub',1,'dispatched',100.25,NULL);
            INSERT INTO operation_events VALUES(2,'original','agent',1,'accepted',104,0);
            INSERT INTO operation_events VALUES(3,'original','agent',2,'waiting_worker',104,1);
            INSERT INTO operation_events VALUES(4,'original','agent',3,'executing',104,40);
        ''')
        db.execute('UPDATE operations SET attempts=?', (attempts,))
    samples = [{'operation_id':'original'}]
    benchmark.enrich(database,samples)
    assert samples[0]['hub_queue_ms']==250
    if attempts == 1:
        assert samples[0]['agent_queue_ms']==40
    else:
        assert 'agent_queue_ms' not in samples[0]
        assert samples[0]['agent_queue_unavailable'] == 'multiple_attempts'
    assert 'payload' not in samples[0]['operation']


@pytest.mark.asyncio
async def test_unknown_without_pending_never_counts_as_success(monkeypatch):
    calls = []
    async def rpc(client, name, args):
        calls.append(args['operation_ids'])
        return {'operations':[{'id':'original', 'pending':False, 'state':'failed',
                               'result':{'ok':False}}]}, False
    monkeypatch.setattr(benchmark, 'rpc', rpc)
    result = await benchmark.finish_original(None,
        {'operation_id':'original', 'state':'unknown', 'pending':False},
        False, time.monotonic()+1)
    assert calls == [['original']] and result[2] and result[4] == 'failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt', [
    {'operations':[]},
    {'operations':[{'id':'other', 'state':'succeeded', 'result':{'ok':True}}]},
    {'operations':[{'id':'original'}]},
])
async def test_invalid_receipt_is_not_a_success(monkeypatch, receipt):
    async def rpc(*args):
        return receipt, False
    monkeypatch.setattr(benchmark, 'rpc', rpc)
    with pytest.raises(ValueError):
        await benchmark.finish_original(None, {'operation_id':'original', 'pending':True},
                                        False, time.monotonic()+1)


@pytest.mark.asyncio
async def test_poll_denied_is_unresolved(monkeypatch):
    async def rpc(*args):
        return {}, True
    monkeypatch.setattr(benchmark, 'rpc', rpc)
    with pytest.raises(RuntimeError, match='poll_denied'):
        await benchmark.finish_original(None, {'operation_id':'original', 'pending':True},
                                        False, time.monotonic()+1)


@pytest.mark.asyncio
async def test_poll_deadline_bounds_hung_http(monkeypatch):
    import asyncio
    async def rpc(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(benchmark, 'rpc', rpc)
    with pytest.raises(TimeoutError):
        await benchmark.finish_original(None, {'operation_id':'original', 'pending':True},
                                        False, time.monotonic()+.01)


@pytest.mark.asyncio
async def test_failed_http_recovers_same_id_and_stops_new_work(monkeypatch, tmp_path):
    from types import SimpleNamespace
    calls = []
    async def rpc(client, name, args):
        calls.append((name, args))
        if name == 'read' and args['path'] == 'README.md':
            return {}, False
        if name == 'read':
            raise httpx.ReadTimeout('SENSITIVE bearer fixture body')
        assert args['operation_ids'] == ['original']
        return {'operations':[{'id':'original', 'state':'succeeded', 'result':{'ok':True, 'data':{}}}]},False
    async def monitor(stack, stop, rows):
        rows.append({'complete':True})
        await stop.wait()
    monkeypatch.setattr(benchmark, 'rpc', rpc)
    monkeypatch.setattr(benchmark, 'resource_samples', monitor)
    monkeypatch.setattr(benchmark, 'enrich', lambda *args:None)
    monkeypatch.setattr(benchmark, 'lookup_original', lambda *args:'original')
    stack = SimpleNamespace(pat='synthetic', url='http://127.0.0.1:1', hubdir=tmp_path)
    summary = await benchmark.workload(stack, tmp_path, 'read', 1, 4, 1)
    rows = [json.loads(line) for line in (tmp_path/'samples.jsonl').read_text().splitlines()]
    assert [name for name, _ in calls] == ['read', 'read', 'task_query']
    assert summary['admission_stopped'] and summary['errors'] == 1
    assert rows[0]['recovered'] and not rows[0]['ok'] and not rows[0]['unresolved']
    assert 'SENSITIVE' not in (tmp_path/'samples.jsonl').read_text()


def test_identity_hashes_nul_names_and_non_source_untracked(monkeypatch, tmp_path):
    path = tmp_path/'odd\nname.bin'
    path.write_bytes(b'first')
    def output(args):
        if 'diff' in args:
            return b'diff'
        if 'status' in args:
            return b'?? odd\nname.bin\0'
        if 'ls-files' in args:
            return b'odd\nname.bin\0'
        return b'a'*40+b'\n'
    monkeypatch.setattr(benchmark.subprocess, 'check_output', output)
    first = benchmark.git_identity(tmp_path)
    path.write_bytes(b'second')
    second = benchmark.git_identity(tmp_path)
    assert first['commit'] == 'a'*40 and first['dirty']
    assert first != second


def test_load_preflight_reports_noise_without_quiet_claim(monkeypatch):
    monkeypatch.setattr(benchmark.os, 'getloadavg', lambda:(80, 75, 70))
    monkeypatch.setattr(benchmark.os, 'cpu_count', lambda:10)
    rows, overloaded = benchmark.preflight(0, 1)
    assert overloaded and rows[0]['loadavg'] == [80, 75, 70]


def test_short_latency_is_separate_and_zero_duration_is_explicit():
    summary = benchmark.summarize([
        {'worker':0, 'kind':'long', 'ok':True, 'latency_ms':100},
        {'worker':1, 'kind':'read', 'ok':False, 'latency_ms':10}], 0)
    assert summary['by_kind']['read']['latency_ms_all']['p95'] == 10
    assert summary['by_kind']['read']['errors'] == 1
    assert summary['throughput_success_per_second'] is None


def test_lookup_original_requires_one_exact_idem(tmp_path):
    path = tmp_path/'db.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE operations(id TEXT,idem TEXT);"
                         "INSERT INTO operations VALUES('a','key');")
    assert benchmark.lookup_original(path, 'key') == 'a'
    with pytest.raises(ValueError):
        benchmark.lookup_original(path, 'absent')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO operations VALUES('b','key')")
    with pytest.raises(ValueError):
        benchmark.lookup_original(path, 'key')


@pytest.mark.asyncio
async def test_warmup_failure_stops_before_resource_or_workload(monkeypatch, tmp_path):
    from types import SimpleNamespace
    async def rpc(*args):
        return {'error':{'code':'DENIED'}}, True
    async def monitor(*args):
        raise AssertionError('measurement must not start')
    monkeypatch.setattr(benchmark, 'rpc', rpc)
    monkeypatch.setattr(benchmark, 'resource_samples', monitor)
    stack = SimpleNamespace(pat='synthetic', url='http://127.0.0.1:1', hubdir=tmp_path)
    with pytest.raises(RuntimeError, match='warmup_failed'):
        await benchmark.workload(stack, tmp_path, 'read', 1, 1, 1)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('state,ok,outer_failed,expected_failed', [
    ('succeeded', True, False, False),
    ('succeeded', False, False, True),
    ('failed', True, False, True),
    ('succeeded', True, True, True),
])
async def test_immediate_durable_receipt_is_unwrapped(state, ok, outer_failed, expected_failed):
    value = {'operation_id':'original', 'state':state, 'pending':False,
             'result':{'ok':ok, 'data':{'exit_code':0}}}
    identifier, result, failed, polls, actual_state = await benchmark.finish_original(
        None, value, outer_failed, time.monotonic()+1)
    assert identifier == 'original' and actual_state == state
    assert result == {'exit_code':0} and polls == 0 and failed is expected_failed


def test_synthetic_task_counts_each_real_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'bench').mkdir()
    command = benchmark.task_command(0, 'count.txt')
    exec(command, {})
    exec(command, {})
    assert (tmp_path/'bench/count.txt').read_text().splitlines() == ['executed', 'executed']


@pytest.mark.asyncio
@pytest.mark.parametrize('effects', [1, 2])
async def test_disconnect_negotiates_accept_and_recovers_one_original(monkeypatch, tmp_path, effects):
    import asyncio
    from types import SimpleNamespace
    database = tmp_path/'hub.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE operations(id TEXT,idem TEXT,state TEXT)')
    (tmp_path/'bench').mkdir()
    counter = tmp_path/'bench/disconnect-count.txt'
    calls = []
    async def rpc(client, name, args):
        assert client.headers['accept'] == 'application/json, text/event-stream'
        calls.append((name, args))
        assert name == 'exec'
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO operations VALUES('original',?,'running')", (args['idempotency_key'],))
        await asyncio.Event().wait()
    async def finish(client, data, failed, deadline):
        assert data == {'pending':True, 'operation_id':'original'} and not failed
        counter.write_text('executed\n'*effects)
        return 'original', {'exit_code':0}, False, 1, 'succeeded'
    monkeypatch.setattr(benchmark, 'rpc', rpc)
    monkeypatch.setattr(benchmark, 'finish_original', finish)
    stack = SimpleNamespace(pat='synthetic', url='http://127.0.0.1:1',
                            hubdir=tmp_path, projectalpha=tmp_path)
    result = await benchmark.disconnect_check(stack)
    assert result['exercised'] and result['operation_rows'] == 1
    assert result['side_effect_count'] == effects and result['passed'] is (effects == 1)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_disconnect_initial_http_failure_is_explicit_and_not_retried(monkeypatch, tmp_path):
    from types import SimpleNamespace
    with sqlite3.connect(tmp_path/'hub.sqlite3') as db:
        db.execute('CREATE TABLE operations(id TEXT,idem TEXT,state TEXT)')
    calls = []
    async def rpc(client, name, args):
        calls.append(name)
        response = httpx.Response(406, request=httpx.Request('POST', 'http://127.0.0.1/mcp'))
        response.raise_for_status()
    monkeypatch.setattr(benchmark, 'rpc', rpc)
    stack = SimpleNamespace(pat='synthetic', url='http://127.0.0.1:1',
                            hubdir=tmp_path, projectalpha=tmp_path)
    result = await benchmark.disconnect_check(stack)
    assert result == {'exercised':False, 'passed':False, 'reason':'initial_HTTPStatusError'}
    assert calls == ['exec']
