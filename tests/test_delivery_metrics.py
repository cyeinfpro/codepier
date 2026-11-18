"""Bounded delivery timing aggregates: classification, splitting and limits."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import hub.delivery_metrics as delivery_metrics
from hub.delivery_metrics import (
    DeliveryMetrics,
    DeliveryTiming,
    classify,
    monitor_loop_lag,
    timed_phase,
)
from tests.test_queue_panel import panel  # noqa: F401


def prepared(*types):
    return ('device', object(), [{'type': kind, 'id': 'op'} for kind in types])


@pytest.mark.parametrize('value,attempts,offline,expected', [
    (None, 0, False, 'settled'),
    (None, 0, True, 'offline_backoff'),
    (prepared('call'), 0, False, 'first_call'),
    (prepared('call'), 2, False, 'retry_call'),
    (prepared('call'), None, False, 'retry_call'),
    (prepared('probe'), 1, False, 'probe'),
    (prepared('cancel'), 1, False, 'cancel'),
    (prepared('cancel_pending', 'probe'), 1, False, 'cancel_pending'),
    (prepared(), 0, False, 'settled'),
])
def test_classification_separates_first_calls_from_recovery(value, attempts, offline, expected):
    assert classify(value, attempts=attempts, offline=offline) == expected


def test_timed_phase_splits_worker_queue_from_run():
    timing = DeliveryTiming('op')
    phase = timed_phase(timing, 'prepare_queue', 'prepare_run', lambda value: value * 2, 21)
    time.sleep(0.01)
    assert phase() == 42
    assert timing.stages['prepare_queue'] >= 5
    assert 0 <= timing.stages['prepare_run'] < timing.stages['prepare_queue']
    failing = timed_phase(timing, 'fence_queue', 'fence_run', lambda: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        failing()
    assert 'fence_run' in timing.stages


def test_candidate_wait_and_total_include_time_since_due():
    timing = DeliveryTiming('op', due=100.0, attempts=0, now=101.5)
    assert timing.stages['candidate_wait'] == pytest.approx(1500)
    metrics = DeliveryMetrics(slow_ms=1000)
    timing.kind = 'first_call'
    metrics.finish(timing)
    snapshot = metrics.snapshot()
    assert snapshot['counts']['first_call'] == 1
    assert snapshot['stages_ms']['first_call']['total']['p50'] >= 1500
    assert snapshot['slow_samples'][0]['operation_id'] == 'op'
    assert snapshot['slow_samples'][0]['kind'] == 'first_call'
    # A due time in the future (clock adjustment) never produces negative waits.
    assert DeliveryTiming('op', due=200.0, now=100.0).stages['candidate_wait'] == 0


def test_total_adds_one_wall_time_delta_to_monotonic_delivery_stages(monkeypatch):
    clock = SimpleNamespace(wall=10.0, mono=25.0)
    monkeypatch.setattr(delivery_metrics, "time", SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.mono,
    ))
    metrics = DeliveryMetrics(slow_ms=0)
    timing = DeliveryTiming("op", due=9.0, attempts=0)
    # The operation was already selected, so a later wall-clock adjustment
    # cannot inflate the candidate wait or total.
    clock.wall = 500.0
    timing.kind = "first_call"
    timing.add("batch_wait", 25.0, 25.25)
    timing.add("slot_wait", 25.25, 25.75)
    clock.mono = 26.0
    metrics.finish(timing)
    stages = metrics.snapshot()["slow_samples"][0]["stages_ms"]
    assert stages == {
        "candidate_wait": 1000.0, "batch_wait": 250.0, "slot_wait": 500.0, "total": 2000.0,
    }


def test_metrics_are_bounded_and_secret_free():
    metrics = DeliveryMetrics(sample_limit=4, slow_limit=2, slow_ms=0)
    for index in range(10):
        timing = DeliveryTiming(f'op-{index}')
        timing.add('prepare_run', time.monotonic())
        timing.add(f'raw-argument-{index}', time.monotonic())
        timing.kind = 'unknown-kind-is-settled'
        metrics.finish(timing)
        metrics.round(index)
        metrics.batch(index, index)
    snapshot = metrics.snapshot()
    assert snapshot['counts']['settled'] == 10
    assert set(snapshot["stages_ms"]["settled"]) == {"prepare_run", "total"}
    assert snapshot['stages_ms']['settled']['prepare_run']['count'] == 4
    assert snapshot['rounds']['count'] == 4 and snapshot['rounds']['candidates']['max'] == 9
    assert [sample['operation_id'] for sample in snapshot['slow_samples']] == ['op-8', 'op-9']
    assert set(snapshot['slow_samples'][0]) == {'operation_id', 'kind', 'at', 'stages_ms'}
    snapshot["slow_samples"][0]["stages_ms"]["result"] = "reader-mutated"
    assert "result" not in metrics.snapshot()["slow_samples"][0]["stages_ms"]
    with pytest.raises(ValueError):
        DeliveryMetrics(sample_limit=0)
    with pytest.raises(ValueError):
        DeliveryMetrics(slow_limit=1000)


def test_current_payload_pressure_and_active_batch_are_visible_before_send(monkeypatch):
    clock = SimpleNamespace(mono=25.0)
    monkeypatch.setattr(delivery_metrics, "time", SimpleNamespace(
        time=lambda: 100, monotonic=lambda: clock.mono,
    ))
    metrics = DeliveryMetrics()
    oldest = metrics.start_batch()
    clock.mono += 1
    newest = metrics.start_batch()
    metrics.retain_payload(10)
    metrics.retain_payload(20)
    metrics.release_payload(10)
    clock.mono += 1
    snapshot = metrics.snapshot()
    assert snapshot["prepared_payloads"] == {
        "measurement": "prepared_request_utf8_bytes_before_slot_release",
        "count": 1, "peak_count": 2, "bytes": 20, "peak_bytes": 30,
    }
    assert snapshot["device_batches"]["active"] == 2
    assert snapshot["device_batches"]["oldest_active_ms"] == 2000

    metrics.release_payload(20)
    metrics.batch(3, 2000, started=oldest)
    assert metrics.snapshot()["device_batches"]["oldest_active_ms"] == 1000
    metrics.batch(4, 1000, started=newest)
    snapshot = metrics.snapshot()
    assert snapshot["device_batches"]["active"] == 0
    assert snapshot["device_batches"]["oldest_active_ms"] is None
    assert snapshot["prepared_payloads"]["bytes"] == 0
    assert snapshot["prepared_payloads"]["peak_bytes"] == 30


def test_diagnostics_measure_selection_and_maintenance_worker_phases():
    metrics = DeliveryMetrics(sample_limit=1)
    metrics.round(18, elapsed_ms=3.0)
    metrics.round(32, elapsed_ms=5.0)
    metrics.maintenance(8.1, queue_ms=1.1, run_ms=7.0)
    snapshot = metrics.snapshot()
    assert snapshot["rounds"]["candidates"]["p50"] == 32
    assert snapshot["rounds"]["select_ms"]["p50"] == 5.0
    assert snapshot["maintenance_ms"]["p50"] == 8.1
    assert snapshot["maintenance_queue_ms"]["p50"] == 1.1
    assert snapshot["maintenance_run_ms"]["p50"] == 7.0


def test_diagnostics_only_retain_fixed_connection_counters():
    metrics = DeliveryMetrics()
    for event in ("connected", "replaced", "fixture-device", "replaced", "disconnected"):
        metrics.connection(event)
    assert metrics.snapshot()["connection_events"] == {
        "connected": 1, "replaced": 2, "disconnected": 1,
    }


def test_cross_tenant_slow_samples_are_only_available_to_instance_admin(panel):
    app, client = panel
    timing = DeliveryTiming("foreign-operation", due=time.time() - 2)
    timing.kind = "first_call"
    timing.payload_bytes = 1234
    app.state.runtime.delivery_metrics.finish(timing)

    admin_response = client.get("/api/queue-diagnostics")
    assert admin_response.status_code == 200, admin_response.text
    sample = admin_response.json()["delivery"]["slow_samples"][0]
    assert sample["operation_id"] == "foreign-operation"
    assert set(sample) == {"operation_id", "kind", "at", "stages_ms"}

    app.state.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    denied = client.get("/api/queue-diagnostics")
    assert denied.status_code == 403
    assert "foreign-operation" not in denied.text
    queue = client.get("/api/operations/queue")
    assert queue.status_code == 200
    assert "delivery" not in queue.json()
    assert "foreign-operation" not in queue.text


def test_loop_lag_monitor_records_scheduling_delay():
    async def run():
        metrics = DeliveryMetrics()
        stop = False
        task = asyncio.create_task(monitor_loop_lag(metrics, lambda: stop, interval=0.01))
        await asyncio.sleep(0.005)
        time.sleep(0.05)  # Block the loop: the next wake-up is late.
        await asyncio.sleep(0.03)
        stop = True
        await asyncio.wait_for(task, 1)
        lag = metrics.snapshot()['loop_lag_ms']
        assert lag['max'] >= 20
    asyncio.run(run())
