"""Validate the isolated sustained-run harness without shortening a soak claim."""
import asyncio
import json

import pytest

from scripts.soak_multidot import RunConfig, SoakHarness, budget_evidence, run


@pytest.mark.asyncio
@pytest.mark.integration
async def test_short_real_http_flow_preserves_every_source_and_excludes_setup(tmp_path):
    output = tmp_path / 'fresh-soak'
    config = RunConfig(duration_seconds=2.2, owners=2, dots_per_owner=2,
                       batch_interval_seconds=1, slow_callback_seconds=.02,
                       fault_every_seconds=0, sample_interval_seconds=1,
                       drain_timeout_seconds=10)
    result = await run(output, config)
    assert result['state'] == 'passed', result['checks']
    assert result['duration_class'] == 'short_self_check'
    assert result['real_dot_host_verified'] is False
    assert result['real_device_verified'] is False
    assert result['measured_seconds'] >= 2.2
    assert result['transport']['kind'] == 'actual_loopback_http_injected_fixture_sender'
    assert result['transport']['challenge_count'] == 4
    assert result['transport']['attempts'] > 0
    assert result['transport']['peak_per_endpoint'] == 1
    assert sum(row['admitted'] for row in result['owners'].values()) == 12
    assert sum(row['replied'] for row in result['owners'].values()) == 12
    assert result['persisted']['source_count'] == 12
    assert result['persisted']['operations'] == 0
    assert result['source_before']['sha256'] == result['source_after_sha256']
    saved = json.loads((output / 'summary.json').read_text())
    assert saved == result
    serialized = json.dumps(saved)
    assert 'whsec_' not in serialized and 'consumer_proof' not in serialized
    assert result['memory']['sample_count'] >= 2
    assert result['store_final']['worker']['quantiles_omitted'] == 'recent window still includes setup'


@pytest.mark.asyncio
async def test_existing_output_is_never_modified_even_on_failure(tmp_path):
    existing = tmp_path / 'existing-hub'
    existing.mkdir()
    marker = existing / 'existing-data'
    marker.write_bytes(b'leave this untouched')
    with pytest.raises(FileExistsError):
        await run(existing, RunConfig(duration_seconds=.1))
    assert marker.read_bytes() == b'leave this untouched'
    assert set(existing.iterdir()) == {marker}


@pytest.mark.parametrize('kwargs', [
    {'duration_seconds': float('nan')}, {'duration_seconds': 1801},
    {'owners': True}, {'owners': 1}, {'batch_interval_seconds': .01},
    {'fault_every_seconds': 2, 'fault_duration_seconds': 8},
])
def test_invalid_load_is_rejected_before_fixture_creation(kwargs):
    with pytest.raises(ValueError):
        RunConfig(**kwargs).validate()


def test_report_checker_rejects_a_fabricated_over_budget_burst():
    valid = [(0., owner, 1000) for owner in range(3) for _ in range(4)]
    assert all(row['within_envelopes'] for row in budget_evidence(valid, 3).values())
    invalid = [(0., 0, 1000)] * 25
    report = budget_evidence(invalid, 3)
    assert not report['global']['within_envelopes']
    assert not report['owner_0']['within_envelopes']


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recovered_lifecycle_failure_cannot_turn_the_report_green(tmp_path):
    harness = SoakHarness(tmp_path / 'lifecycle-failure', RunConfig(
        duration_seconds=.1, owners=2, dots_per_owner=1,
        fault_every_seconds=0, sample_interval_seconds=1, drain_timeout_seconds=10))
    extra_loop = None
    try:
        await harness.setup()
        await harness.produce()
        persisted = await harness.drain()
        recovered = asyncio.Event()
        calls = 0

        async def fails_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyError('fixture-secret-must-not-enter-report')
            recovered.set()

        # Exercise the actual catch/log/recover path. Its short interval is only
        # for this negative evidence check; the sustained runner uses start().
        extra_loop = asyncio.create_task(harness.service.loops.loop('reconcile', fails_once, .001))
        await asyncio.wait_for(recovered.wait(), 2)
        assert calls >= 2
        assert harness.service.health['reconcile']['status'] == 'running'
        extra_loop.cancel()
        await asyncio.gather(extra_loop, return_exceptions=True)
        extra_loop = None
        harness.sampling_done.set()
        await asyncio.gather(*(task for task in harness.tasks if task.get_name() == 'soak-sample'))
        await harness.service.loops.stop()
        assert harness.service.health['reconcile']['status'] == 'stopped'
        report = await harness.result(persisted)
        assert report['state'] == 'failed'
        assert report['checks']['no_unexpected_errors'] is False
        assert report['checks']['lifecycle_failures_observable'] is True
        assert report['lifecycle_warning_count'] == 1
        assert {'component': 'lifecycle:reconcile', 'type': 'KeyError', 'code': ''} in report['errors']
        assert 'fixture-secret-must-not-enter-report' not in json.dumps(report)
    finally:
        if extra_loop is not None:
            extra_loop.cancel()
            await asyncio.gather(extra_loop, return_exceptions=True)
        await harness.close()
    assert harness.lifecycle_observer not in harness.lifecycle_logger.handlers
