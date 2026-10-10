"""Pure mock harness checks only: no listener, Hub, Agent or heavy benchmark."""
import asyncio
import json

import httpx
import pytest

from hub.gateway import remote
from scripts import benchmark_gateway as benchmark


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,peak', [('modern',8), ('legacy',1)])
@pytest.mark.parametrize('workload', ['read','read_write','long_short'])
async def test_mock_phase_preserves_raw_counts_and_protocol_bound(monkeypatch, protocol, peak, workload):
    async def forbidden(*args, **kwargs):
        raise AssertionError('mock must never start a listener')
    monkeypatch.setattr(asyncio, 'start_server', forbidden)
    summary, samples, resources, events = await benchmark.run_phase(remote,
        transport_name='mock', protocol=protocol, workload=workload, concurrency=8,
        operations=2, short_seconds=.001, long_seconds=.002, sample_resources=False)
    assert summary['complete'] and len(samples) == summary['backend_requests'] == 16
    assert summary['backend_peak_active'] == peak
    assert all(row['queue_ms'] is not None for row in samples)
    assert summary['cancellation']['original_received'] == 1
    assert summary['cancellation']['original_outcomes'] == ['cancelled_in_mock']
    assert sum(e['number'] == -2 for e in events) == 1
    assert sum(e['number'] == -3 for e in events) == 1
    assert all(e['number'] != -1 for e in events)
    assert resources == [] and summary['resource_sampling_complete'] is None
    assert summary['connections_after_workload'] == 0
    assert summary['measured_synthetic_write_count'] == (8 if workload == 'read_write' else 0)
    assert 'Authorization' not in json.dumps(events)
    if workload == 'long_short':
        assert summary['by_kind']['read']['samples'] == 15
        assert summary['by_kind']['long']['samples'] == 1


@pytest.mark.asyncio
async def test_failure_is_retained_and_halts_without_replay(monkeypatch):
    original = benchmark.Backend.respond
    async def failing(self, body, connection=None):
        if body['method'] == 'tools/call' and body['params']['arguments']['number'] == 0:
            raise httpx.ReadTimeout('SENSITIVE synthetic request body')
        return await original(self, body, connection)
    monkeypatch.setattr(benchmark.Backend, 'respond', failing)
    summary, samples, _, events = await benchmark.run_phase(remote,
        transport_name='mock', protocol='modern', workload='read', concurrency=1,
        operations=4, short_seconds=0, long_seconds=0, sample_resources=False)
    assert not summary['complete'] and summary['admission_stopped']
    assert summary['errors'] == 1 and len(samples) == 1
    assert samples[0]['error'] == 'BackendError'
    assert not summary['cancellation']['exercised'] and events == []
    assert 'SENSITIVE' not in json.dumps(summary)+json.dumps(samples)


def test_baseline_sha_checked_before_import(tmp_path):
    path = tmp_path/'remote.py'
    path.write_text("raise AssertionError('must not import')")
    with pytest.raises(ValueError, match='sha256_mismatch'):
        benchmark.load_module(path, expected_sha256=benchmark.BASELINE_SHA256)


def test_workload_assignment_is_deterministic():
    assert benchmark.kind_for('read_write', 5, 0) == 'write'
    assert benchmark.kind_for('read_write', 5, 1) == 'read'
    assert benchmark.kind_for('long_short', 8, 0) == 'long'
    assert benchmark.kind_for('long_short', 9, 0) == 'read'


def test_jsonl_is_independently_parseable(tmp_path):
    path = tmp_path/'raw.jsonl'
    benchmark.write_rows(path, [{'number':1}, {'number':2}])
    assert [json.loads(row) for row in path.read_text().splitlines()] == [{'number':1}, {'number':2}]
