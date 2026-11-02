"""Pure admission-observation regressions: no Hub, Agent, listener or benchmark."""
import copy
import hashlib
import json
import math
from types import SimpleNamespace

import httpx
import pytest

from scripts import benchmark_hub_agent as benchmark


SENSITIVE = 'SENSITIVE synthetic bearer /private/fixture/path'


class FakeClock:
    def __init__(self):
        self.start = self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert 0 <= seconds <= 3
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def scheduler_payload(*, state='applied', reason='adaptive', node=224,
                      project=196, paused=False):
    return {
        'device_id': 'fixture-device',
        'state': state,
        'revision': 'a' * 64,
        'config': {'adaptive': True, 'project_limits': {}},
        'stale_project_limits': [],
        'durable_admission': {
            'node_pending_limit': node,
            'project_pending_limit': project,
            'reason': reason,
            'new_heavy_admission_paused': paused,
            'overload_read_reserve': 0,
            'counts_include': ['queued', 'running', 'reconnecting', 'cancelling'],
        },
        'reported': {
            'scope': 'node',
            'reason': 'healthy',
            'running': 3,
            'queued': 0,
            'lanes': {
                'execution': {'capacity': 8, 'running': 1, 'queued': 0},
                'read': {'capacity': 8, 'running': 2, 'queued': 0},
                'remote': {'capacity': 12, 'running': 0, 'queued': 0},
            },
        },
    }


def scripted_probe(clock, responses):
    remaining = iter(responses)
    calls = []

    def probe(*, timeout):
        calls.append((clock() - clock.start, timeout))
        value = next(remaining)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    return probe, calls


def test_snapshot_preserves_only_documented_safe_fields():
    raw = scheduler_payload()
    expected = {
        'state': 'applied',
        'revision': 'a' * 64,
        'durable_admission': {
            'reason': 'adaptive',
            'node_pending_limit': 224,
            'project_pending_limit': 196,
            'new_heavy_admission_paused': False,
            'overload_read_reserve': 0,
        },
        'reported': copy.deepcopy(raw['reported']),
    }
    expected['reported'].pop('scope')
    raw['config'] = {'secret': SENSITIVE}
    raw['device_id'] = raw['stale_project_limits'] = raw['extra'] = SENSITIVE
    raw['durable_admission']['extra'] = SENSITIVE
    raw['reported']['extra'] = SENSITIVE
    raw['reported']['lanes']['private'] = {'path': SENSITIVE}
    raw['reported']['lanes']['read']['secret'] = SENSITIVE
    original = copy.deepcopy(raw)

    snapshot = benchmark.admission_snapshot(raw)

    assert snapshot == expected
    assert raw == original
    assert SENSITIVE not in json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize('invalid', [SENSITIVE, [SENSITIVE], {'secret': SENSITIVE}, True, None])
def test_snapshot_unknown_enums_and_revision_never_echo_untrusted_values(invalid):
    raw = scheduler_payload(state=invalid, reason=invalid)
    raw['revision'] = invalid
    raw['reported']['reason'] = invalid
    snapshot = benchmark.admission_snapshot(raw)

    assert snapshot['state'] == 'unknown'
    assert snapshot['revision'] is None
    assert snapshot['durable_admission']['reason'] == 'unknown'
    assert snapshot['reported']['reason'] == 'unknown'
    assert not benchmark.admission_ready(snapshot, 128)
    assert SENSITIVE not in json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize('invalid', [True, False, -1, 0, 1.5, '128', None,
                                     float('nan'), float('inf'), 2**2048])
def test_snapshot_invalid_pending_limits_cannot_be_ready(invalid):
    snapshot = benchmark.admission_snapshot(scheduler_payload(node=invalid, project=invalid))
    assert snapshot['durable_admission']['node_pending_limit'] is None
    assert snapshot['durable_admission']['project_pending_limit'] is None
    assert not benchmark.admission_ready(snapshot, 128)
    json.dumps(snapshot, allow_nan=False)


def test_snapshot_invalid_reported_counts_and_hard_cap_overflow_are_null():
    raw = scheduler_payload(node=513, project=257, paused='false')
    raw['durable_admission']['overload_read_reserve'] = 9
    raw['reported']['running'] = float('nan')
    raw['reported']['queued'] = -1
    raw['reported']['lanes']['execution']['capacity'] = True
    raw['reported']['lanes']['read']['running'] = SENSITIVE
    raw['reported']['lanes']['remote']['queued'] = float('inf')
    snapshot = benchmark.admission_snapshot(raw)

    for key in ('node_pending_limit', 'project_pending_limit',
                'new_heavy_admission_paused', 'overload_read_reserve'):
        assert snapshot['durable_admission'][key] is None
    assert snapshot['reported']['running'] is None
    assert snapshot['reported']['queued'] is None
    assert snapshot['reported']['lanes']['execution']['capacity'] is None
    assert snapshot['reported']['lanes']['read']['running'] is None
    assert snapshot['reported']['lanes']['remote']['queued'] is None
    assert SENSITIVE not in json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize('value', [None, [], SENSITIVE, 1])
def test_nonobject_scheduler_response_is_not_ready(value):
    snapshot = benchmark.admission_snapshot(value)
    assert not benchmark.admission_ready(snapshot, 128)
    assert SENSITIVE not in json.dumps(snapshot, allow_nan=False)


def test_ready_requires_both_pending_limits_at_the_requested_threshold():
    snapshot = benchmark.admission_snapshot(scheduler_payload(node=128, project=128))
    assert benchmark.admission_ready(snapshot, 128)
    assert not benchmark.admission_ready(snapshot, 129)
    assert not benchmark.admission_ready(scheduler_payload(node=127), 128)
    assert not benchmark.admission_ready(scheduler_payload(project=127), 128)


@pytest.mark.parametrize('overrides', [
    {'state': 'pending'}, {'state': 'unknown'}, {'reason': 'legacy'},
    {'reason': 'unavailable'}, {'reason': 'pressure'}, {'paused': True},
    {'paused': None}, {'node': True}, {'project': '196'},
])
def test_ready_rechecks_untrusted_values_instead_of_trusting_a_snapshot(overrides):
    assert not benchmark.admission_ready(scheduler_payload(**overrides), 128)


@pytest.mark.parametrize('required', [0, 257, True, 128.0, '128'])
def test_ready_rejects_invalid_required_pending(required):
    assert not benchmark.admission_ready(scheduler_payload(), required)


def test_ready_requires_a_valid_scheduler_revision():
    raw = scheduler_payload()
    for revision in (None, '', 'A' * 64, SENSITIVE):
        raw['revision'] = revision
        assert not benchmark.admission_ready(raw, 128)


@pytest.mark.parametrize('reason,expected_ready', [('pressure', False), ('adaptive', True)])
def test_cold_is_the_default_and_observes_once_without_waiting(reason, expected_ready):
    clock = FakeClock()
    probe, calls = scripted_probe(clock, [scheduler_payload(reason=reason)])

    observation = benchmark.prepare_admission(probe, 128, clock=clock, sleep=clock.sleep)

    assert observation['profile'] == 'cold'
    assert observation['required_pending'] == 128
    assert observation['timeout_seconds'] == 300
    assert observation['elapsed_seconds'] == 0
    assert observation['ready'] is expected_ready
    assert observation['probe_count'] == len(observation['observations']) == 1
    assert observation['observations'][0]['elapsed_seconds'] == 0
    assert observation['observations'][0]['error'] is None
    assert observation['observations'][0]['snapshot']['durable_admission']['reason'] == reason
    assert calls == [(0, 10)]
    assert clock.sleeps == []


def test_cold_probe_failure_is_recorded_without_waiting_or_exposing_error_text():
    clock = FakeClock()
    probe, calls = scripted_probe(clock, [httpx.ReadTimeout(SENSITIVE)])
    observation = benchmark.prepare_admission(probe, 128, clock=clock, sleep=clock.sleep)

    assert not observation['ready']
    assert observation['probe_count'] == 1
    assert observation['observations'] == [
        {'elapsed_seconds': 0, 'snapshot': None, 'error': 'ReadTimeout'},
    ]
    assert calls == [(0, 10)] and clock.sleeps == []
    assert SENSITIVE not in json.dumps(observation, allow_nan=False)


def test_ready_waits_through_unknown_and_pressure_with_bounded_probes():
    clock = FakeClock()
    probe, calls = scripted_probe(clock, [
        scheduler_payload(state='unknown', reason='unavailable', node=64, project=32),
        scheduler_payload(reason='pressure', node=64, project=32),
        scheduler_payload(),
    ])
    observation = benchmark.prepare_admission(
        probe, 128, profile='ready', timeout=8, clock=clock, sleep=clock.sleep)

    assert observation['profile'] == 'ready' and observation['ready']
    assert observation['required_pending'] == 128 and observation['timeout_seconds'] == 8
    assert observation['elapsed_seconds'] == 6
    assert observation['probe_count'] == 3
    assert calls == [(0, 8), (3, 5), (6, 2)]
    assert clock.sleeps == [3, 3]
    assert [row['elapsed_seconds'] for row in observation['observations']] == [0, 3, 6]
    assert [row['snapshot']['durable_admission']['reason']
            for row in observation['observations']] == ['unavailable', 'pressure', 'adaptive']
    assert all(row['error'] is None for row in observation['observations'])


@pytest.mark.parametrize('reason', ['unavailable', 'pressure'])
@pytest.mark.parametrize('timeout', [10, 300])
def test_ready_permanently_unavailable_or_pressured_hits_absolute_deadline(reason, timeout):
    clock = FakeClock()
    calls = []

    def probe(*, timeout):
        calls.append((clock() - clock.start, timeout))
        return scheduler_payload(state='unknown' if reason == 'unavailable' else 'applied',
                                 reason=reason, node=64, project=32)

    with pytest.raises(benchmark.AdmissionReadinessError) as caught:
        benchmark.prepare_admission(probe, 128, profile='ready', timeout=timeout,
                                    clock=clock, sleep=clock.sleep)

    observation = caught.value.observation
    assert not observation['ready'] and observation['profile'] == 'ready'
    assert observation['elapsed_seconds'] == timeout
    assert observation['probe_count'] == len(calls) == math.ceil(timeout / 3)
    assert len(observation['observations']) == len(calls)
    assert [at for at, _ in calls] == list(range(0, timeout, 3))
    assert all(request_timeout == min(10, timeout - at) for at, request_timeout in calls)
    assert all(0 < request_timeout <= 10 and at < timeout for at, request_timeout in calls)


@pytest.mark.parametrize('response_duration', [10, 10.25])
def test_ready_response_at_or_after_deadline_cannot_pass(response_duration):
    clock = FakeClock()
    calls = []

    def probe(*, timeout):
        calls.append(timeout)
        clock.advance(response_duration)
        return scheduler_payload()

    with pytest.raises(benchmark.AdmissionReadinessError) as caught:
        benchmark.prepare_admission(probe, 128, profile='ready', timeout=10,
                                    clock=clock, sleep=clock.sleep)

    assert calls == [10] and clock.sleeps == []
    assert caught.value.observation['ready'] is False
    assert caught.value.observation['elapsed_seconds'] >= 10
    assert caught.value.observation['probe_count'] == 1


def test_deadline_crossing_between_clock_reads_cannot_issue_a_zero_timeout_probe():
    ticks = iter([0, 0, 1, 1])
    calls = []

    def clock():
        return next(ticks, 1)

    def probe(*, timeout):
        calls.append(timeout)
        return scheduler_payload()

    def forbidden_sleep(seconds):
        raise AssertionError('a reached deadline must not sleep')

    with pytest.raises(benchmark.AdmissionReadinessError) as caught:
        benchmark.prepare_admission(probe, 128, profile='ready', timeout=1,
                                    clock=clock, sleep=forbidden_sleep)
    assert calls == []
    assert caught.value.observation['probe_count'] == 0
    assert caught.value.observation['elapsed_seconds'] == 1
    assert not caught.value.observation['ready']


def test_ready_probe_failure_clears_the_snapshot_and_keeps_only_the_exception_class():
    clock = FakeClock()
    probe, calls = scripted_probe(clock, [
        scheduler_payload(reason='pressure', node=64, project=32),
        httpx.ReadTimeout(SENSITIVE),
        scheduler_payload(),
    ])
    observation = benchmark.prepare_admission(
        probe, 128, profile='ready', timeout=10, clock=clock, sleep=clock.sleep)

    assert observation['ready'] and observation['probe_count'] == len(calls) == 3
    assert observation['observations'][0]['snapshot'] is not None
    assert observation['observations'][1] == {
        'elapsed_seconds': 3, 'snapshot': None, 'error': 'ReadTimeout',
    }
    assert observation['observations'][2]['error'] is None
    assert SENSITIVE not in json.dumps(observation, allow_nan=False)


def test_slow_probes_consume_the_same_deadline_and_respect_minimum_spacing():
    clock = FakeClock()
    calls = []

    def probe(*, timeout):
        calls.append((clock() - clock.start, timeout))
        clock.advance(1.25)
        return scheduler_payload(reason='pressure', node=64, project=32)

    with pytest.raises(benchmark.AdmissionReadinessError) as caught:
        benchmark.prepare_admission(probe, 128, profile='ready', timeout=10,
                                    clock=clock, sleep=clock.sleep)

    assert len(calls) >= 2
    assert all(later[0] - earlier[0] >= 3 for earlier, later in zip(calls, calls[1:]))
    assert all(0 <= at < 10 and request_timeout == min(10, 10 - at)
               for at, request_timeout in calls)
    assert caught.value.observation['elapsed_seconds'] >= 10
    assert caught.value.observation['probe_count'] == len(calls)
    assert not caught.value.observation['ready']


@pytest.mark.parametrize('timeout', [0, -1, 300.001, float('nan'), float('inf')])
def test_invalid_readiness_deadline_is_rejected_before_probing(timeout):
    clock = FakeClock()
    probe, calls = scripted_probe(clock, [scheduler_payload()])
    with pytest.raises(ValueError):
        benchmark.prepare_admission(probe, 128, profile='ready', timeout=timeout,
                                    clock=clock, sleep=clock.sleep)
    assert calls == [] and clock.sleeps == []


def test_observe_uses_only_authenticated_scheduler_get_and_requested_http_timeout():
    raw = scheduler_payload()
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.path == '/api/settings/scheduler'
        assert dict(request.url.params) == {'device': 'fixture-device'}
        assert request.headers['x-fixture-auth'] == 'synthetic-admin'
        assert 'session=synthetic-admin-cookie' in request.headers['cookie']
        assert set(request.extensions['timeout'].values()) == {4.25}
        return httpx.Response(200, json=raw)

    with httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(handle),
                      headers={'X-Fixture-Auth': 'synthetic-admin'},
                      cookies={'session': 'synthetic-admin-cookie'}, trust_env=False) as client:
        stack = SimpleNamespace(client=client, device='fixture-device')
        result = benchmark.observe_admission(stack, timeout=4.25)

    assert result == raw
    assert len(calls) == 1


def test_observe_checks_http_status_before_decoding_a_response_body():
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(503, text=SENSITIVE)

    with httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(handle),
                      trust_env=False) as client:
        stack = SimpleNamespace(client=client, device='fixture-device')
        with pytest.raises(httpx.HTTPStatusError):
            benchmark.observe_admission(stack)
    assert calls == ['GET']


def test_rejection_metadata_keeps_only_safe_flat_fields():
    result = {'error': {
        'code': 'PROJECT_BUSY', 'message': SENSITIVE,
        'queue_scope': 'project', 'queue_reason': 'admission_count',
        'retry_after_seconds': 3, 'admitted': False, 'secret': SENSITIVE,
        'nested': {'queue_scope': 'hub', 'secret': SENSITIVE},
    }, 'secret': SENSITIVE}
    metadata = benchmark.rejection_metadata(result)
    assert metadata == {
        'queue_scope': 'project', 'queue_reason': 'admission_count',
        'retry_after_seconds': 3, 'admitted': False,
    }
    assert SENSITIVE not in json.dumps(metadata, allow_nan=False)


@pytest.mark.parametrize('scope,reason', [
    ('project', 'admission_count'), ('node', 'admission_bytes'),
    ('node', 'control_reserve'), ('node', 'resource_pressure'),
    ('hub', 'project_fairness'),
])
@pytest.mark.parametrize('retry', [0, 1.5, 300])
def test_rejection_metadata_preserves_whitelisted_enums_and_finite_retry_bounds(scope, reason, retry):
    metadata = {'queue_scope': scope, 'queue_reason': reason,
                'retry_after_seconds': retry, 'admitted': True}
    assert benchmark.rejection_metadata({'error': metadata}) == metadata


@pytest.mark.parametrize('invalid', [True, -1, 300.001, '3', None,
                                     float('nan'), float('inf'), 2**2048])
def test_rejection_metadata_drops_unknown_fields_and_invalid_values(invalid):
    result = {'error': {
        'queue_scope': SENSITIVE, 'queue_reason': [SENSITIVE],
        'retry_after_seconds': invalid, 'admitted': 'false',
        'message': SENSITIVE, 'detail': {'admitted': False},
    }}
    assert benchmark.rejection_metadata(result) == {}


@pytest.mark.parametrize('value', [None, [], SENSITIVE, {}, {'error': None}, {'error': []}])
def test_rejection_metadata_requires_an_error_object(value):
    assert benchmark.rejection_metadata(value) == {}


@pytest.mark.asyncio
async def test_workload_keeps_busy_codes_and_counts_and_continues_without_replay(monkeypatch, tmp_path):
    calls = []
    rejections = [
        ('PROJECT_BUSY', 'project', 'admission_count'),
        ('DEVICE_BUSY', 'node', 'admission_bytes'),
        ('HUB_BUSY', 'hub', 'admission_count'),
    ]
    initial_hash = hashlib.sha256(b'initial\n').hexdigest()

    async def rpc(client, name, args):
        calls.append((name, dict(args)))
        assert name == 'read'
        if args['path'] == 'README.md':
            return {}, False
        step = len(calls) - 2
        if step < len(rejections):
            code, scope, reason = rejections[step]
            return {'error': {
                'code': code, 'message': SENSITIVE, 'queue_scope': scope,
                'queue_reason': reason, 'retry_after_seconds': 3,
                'admitted': False, 'secret': SENSITIVE,
            }}, True
        return {'sha256': initial_hash}, False

    async def monitor(stack, stop, rows):
        rows.append({'complete': True})
        await stop.wait()

    def forbidden_recovery(*args):
        raise AssertionError('a known non-admitted response must not recover or replay')

    def forbidden_http(request):
        raise AssertionError('mock RPC must never issue real HTTP')

    client_type = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(forbidden_http)
        return client_type(*args, **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', mock_client)
    monkeypatch.setattr(benchmark, 'rpc', rpc)
    monkeypatch.setattr(benchmark, 'resource_samples', monitor)
    monkeypatch.setattr(benchmark, 'enrich', lambda *args: None)
    monkeypatch.setattr(benchmark, 'lookup_original', forbidden_recovery)
    stack = SimpleNamespace(pat='synthetic', url='http://127.0.0.1:1', hubdir=tmp_path)

    summary = await benchmark.workload(stack, tmp_path, 'read', 1, 4, 1)
    text = (tmp_path / 'samples.jsonl').read_text()
    rows = [json.loads(line) for line in text.splitlines()]

    assert len(calls) == 5
    assert len({args['idempotency_key'] for _, args in calls}) == 5
    assert [args['path'] for _, args in calls] == ['README.md'] + ['bench/worker-0.txt'] * 4
    assert [row['step'] for row in rows] == [0, 1, 2, 3]
    assert all(row['worker'] == 0 and row['polls'] == 0 for row in rows)
    assert [row['error'] for row in rows] == ['PROJECT_BUSY', 'DEVICE_BUSY', 'HUB_BUSY', None]
    assert [row['ok'] for row in rows] == [False, False, False, True]
    assert [row['state'] for row in rows] == ['failed', 'failed', 'failed', 'succeeded']
    for row, (_, scope, reason) in zip(rows, rejections):
        assert row['admission_rejection'] == {
            'queue_scope': scope, 'queue_reason': reason,
            'retry_after_seconds': 3, 'admitted': False,
        }
    assert rows[-1].get('admission_rejection', {}) == {}
    assert summary['samples'] == summary['expected_samples'] == 4
    assert summary['succeeded'] == 1 and summary['errors'] == 3
    assert summary['error_rate'] == .75
    assert summary['by_kind']['read']['errors'] == 3
    assert not summary['admission_stopped'] and summary['unresolved'] == 0
    assert summary['resource_sampling_complete']
    assert SENSITIVE not in text + json.dumps(summary, allow_nan=False)
