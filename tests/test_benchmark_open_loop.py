"""Deterministic open-loop scheduling/statistics checks; no Hub, Agent or listener."""

import argparse
import asyncio
import hashlib
import heapq
import itertools
import json
from types import SimpleNamespace

import httpx
import pytest

from scripts import benchmark_hub_agent as benchmark


INITIAL_SHA = hashlib.sha256(b"initial\n").hexdigest()


class VirtualClock:
    """Virtual time that advances only once every task is waiting on it."""

    def __init__(self, oversleep=0.0):
        self.now = 0.0
        self.oversleep = oversleep
        self.waiters = []
        self.order = itertools.count()

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self.waiters, (self.now + max(0.0, delay), next(self.order), future))
        await future

    async def late_sleep(self, delay):
        # A sender that always wakes late, like a busy event loop.
        await self.sleep(delay + self.oversleep)

    async def run(self, coroutine):
        task = asyncio.ensure_future(coroutine)
        while True:
            for _ in range(100):
                await asyncio.sleep(0)
            if task.done():
                return task.result()
            assert self.waiters, "deadlock: nothing waits on virtual time"
            when, _, future = heapq.heappop(self.waiters)
            self.now = max(self.now, when)
            future.set_result(None)


def fake_service(clock, seconds, *, calls=None):
    async def run(record, lane):
        if calls is not None:
            calls.append(record["sequence"])
        await clock.sleep(seconds(record) if callable(seconds) else seconds)
        record["ok"] = True

    return run


async def drive(clock, schedule, lanes, run, *, abort=None, emit=None, sleep=None):
    return await clock.run(
        benchmark.drive_open_loop(
            schedule,
            [benchmark.FileLane(index) for index in range(lanes)],
            run,
            abort=abort or asyncio.Event(),
            kind_for=lambda sequence: "read",
            emit=emit or benchmark.ignore_record,
            clock=clock,
            sleep=sleep or clock.sleep,
        )
    )


def test_schedule_is_planned_up_front_from_rate():
    planned = benchmark.arrival_schedule(10, operations=5)
    assert [a.sequence for a in planned] == [0, 1, 2, 3, 4]
    assert [a.offset for a in planned] == pytest.approx([0, 0.1, 0.2, 0.3, 0.4])
    assert {a.phase for a in planned} == {"steady"}
    assert len(benchmark.arrival_schedule(4, duration=1)) == 4
    # Rounded before ceil: 10/s for 0.3s is three arrivals, not four.
    assert len(benchmark.arrival_schedule(10, duration=0.3)) == 3
    assert len(benchmark.arrival_schedule(4, duration=2.6)) == 11


def test_burst_is_extra_work_at_t0_then_steady_rate():
    planned = benchmark.arrival_schedule(10, operations=5, burst=3)
    assert [a.phase for a in planned] == ["burst"] * 3 + ["steady"] * 2
    assert [a.offset for a in planned] == pytest.approx([0, 0, 0, 0, 0.1])
    assert [a.sequence for a in planned] == [0, 1, 2, 3, 4]
    assert len(benchmark.arrival_schedule(2, duration=3, burst=4)) == 10


@pytest.mark.parametrize(
    "rate,kwargs",
    [
        (0, {"operations": 1}),
        (float("nan"), {"operations": 1}),
        (float("inf"), {"operations": 1}),
        ("10", {"operations": 1}),
        (10, {}),
        (10, {"operations": 1, "duration": 1}),
        (10, {"operations": 2, "burst": 3}),
        (10, {"operations": 1, "burst": -1}),
        (10, {"operations": 0}),
        (10, {"duration": 0}),
        (10, {"operations": 1.5}),
        (1000, {"duration": 101}),
    ],
)
def test_invalid_schedule_is_refused(rate, kwargs):
    with pytest.raises(ValueError):
        benchmark.arrival_schedule(rate, **kwargs)


@pytest.mark.asyncio
async def test_under_capacity_every_arrival_is_sent_on_time():
    clock = VirtualClock()
    records = await drive(
        clock, benchmark.arrival_schedule(10, operations=5), 2, fake_service(clock, 0.05)
    )
    assert all(r["ok"] for r in records)
    assert [r["send_lag_ms"] for r in records] == pytest.approx([0] * 5)
    assert [r["latency_ms"] for r in records] == pytest.approx([50] * 5)
    assert [r["service_ms"] for r in records] == pytest.approx([50] * 5)
    assert [r["completed_offset_ms"] for r in records] == pytest.approx([50, 150, 250, 350, 450])


@pytest.mark.asyncio
async def test_full_client_drops_instead_of_delaying_later_arrivals():
    clock = VirtualClock()
    emitted, calls = [], []

    def emit(record):
        emitted.append((record["sequence"], clock()))

    records = await drive(
        clock,
        benchmark.arrival_schedule(10, operations=5),
        2,
        fake_service(clock, 1.0, calls=calls),
        emit=emit,
    )
    assert calls == [0, 1]
    assert [r.get("client_dropped", False) for r in records] == [False, False, True, True, True]
    assert {r["error"] for r in records[2:]} == {"client_in_flight_limit"}
    assert all("send_lag_ms" not in r for r in records[2:])
    # Each drop is recorded at its own planned time, before any completion:
    # the sender never waited for capacity (no coordinated omission).
    assert [seq for seq, _ in emitted] == [2, 3, 4, 0, 1]
    assert [at for _, at in emitted] == pytest.approx([0.2, 0.3, 0.4, 1.0, 1.1])
    assert [r["latency_ms"] for r in records[:2]] == pytest.approx([1000, 1000])

    summary = benchmark.summarize_open_loop(records, arrival_rate=10, burst=0, max_in_flight=2)
    assert summary["mode"] == "open_loop"
    assert summary["succeeded"] == 2 and summary["client_dropped"] == 3
    assert summary["errors"] == 3 and summary["sent"] == 2
    assert summary["duration_seconds"] == pytest.approx(1.1)
    assert summary["throughput_success_per_second"] == pytest.approx(2 / 1.1)
    assert summary["max_in_flight_observed"] == 2
    assert summary["oldest_backlog_age_ms_max"] == pytest.approx(1000)
    assert summary["backlog_at_last_arrival"] == {"in_flight": 2, "oldest_age_ms": 400}
    assert summary["offered_rate_mean_per_second"] == pytest.approx(10)


@pytest.mark.asyncio
async def test_latency_starts_at_planned_arrival_not_actual_send():
    clock = VirtualClock(oversleep=0.03)
    records = await drive(
        clock,
        benchmark.arrival_schedule(10, operations=4),
        4,
        fake_service(clock, 0.05),
        sleep=clock.late_sleep,
    )
    assert [r["send_lag_ms"] for r in records] == pytest.approx([0, 30, 30, 30])
    assert [r["service_ms"] for r in records] == pytest.approx([50] * 4)
    assert [r["latency_ms"] for r in records] == pytest.approx([50, 80, 80, 80])
    summary = benchmark.summarize_open_loop(records, arrival_rate=10, burst=0, max_in_flight=4)
    assert summary["send_lag_ms"] == {"p50": 30, "p95": 30, "p99": 30}
    assert summary["send_lag_ms_max"] == pytest.approx(30)
    assert summary["latency_ms_from_scheduled_success"]["p99"] == pytest.approx(80)


@pytest.mark.asyncio
async def test_burst_drain_time_requires_every_burst_operation():
    clock = VirtualClock()
    schedule = benchmark.arrival_schedule(10, operations=6, burst=4)
    records = await drive(clock, schedule, 8, fake_service(clock, 0.3))
    drain = benchmark.summarize_open_loop(records, arrival_rate=10, burst=4, max_in_flight=8)[
        "burst_drain"
    ]
    assert drain == {
        "operations": 4,
        "completed": 4,
        "succeeded": 4,
        "all_completed": True,
        "drain_ms": 300,
    }

    clock = VirtualClock()
    records = await drive(clock, schedule, 3, fake_service(clock, 0.3))
    drain = benchmark.burst_drain(records)
    assert drain["completed"] == 3 and not drain["all_completed"]
    assert drain["drain_ms"] is None
    assert benchmark.burst_drain([r for r in records if r["phase"] == "steady"]) is None


@pytest.mark.asyncio
async def test_stop_after_unexpected_failure_sends_nothing_more():
    clock = VirtualClock()
    abort = asyncio.Event()
    calls = []

    async def run(record, lane):
        calls.append(record["sequence"])
        if record["sequence"] == 1:
            abort.set()
        await clock.sleep(0.01)

    records = await drive(clock, benchmark.arrival_schedule(10, operations=5), 4, run, abort=abort)
    assert calls == [0, 1]
    assert [r.get("not_sent") for r in records] == [None, None] + ["admission_stopped"] * 3
    # The stop is seen when the sender wakes for arrival 2; later arrivals
    # are recorded without waiting for their planned times.
    assert clock.now == pytest.approx(0.2)
    summary = benchmark.summarize_open_loop(records, arrival_rate=10, burst=0, max_in_flight=4)
    assert summary["not_sent_after_stop"] == 3 and summary["sent"] == 2


@pytest.mark.asyncio
async def test_lane_is_never_shared_by_overlapping_operations():
    clock = VirtualClock()
    active = set()

    async def run(record, lane):
        assert lane.index not in active
        active.add(lane.index)
        await clock.sleep((record["sequence"] % 7) * 0.013)
        active.discard(lane.index)
        record["ok"] = True

    records = await drive(clock, benchmark.arrival_schedule(100, operations=60), 3, run)
    sent = [r for r in records if "send_lag_ms" in r]
    dropped = [r for r in records if r.get("client_dropped")]
    assert len(sent) + len(dropped) == 60 and sent and dropped
    assert benchmark.peak_in_flight(records) <= 3
    assert {r["lane"] for r in sent} <= {0, 1, 2}


def sent_sample(sequence, scheduled, lag, service, *, ok=True, error=None, kind="read"):
    return {
        "sequence": sequence,
        "phase": "steady",
        "kind": kind,
        "scheduled_offset_ms": scheduled,
        "send_lag_ms": lag,
        "service_ms": service,
        "latency_ms": lag + service,
        "completed_offset_ms": scheduled + lag + service,
        "ok": ok,
        "error": error,
        "polls": 1,
    }


def test_outcomes_are_counted_separately_and_never_hidden():
    samples = [
        sent_sample(0, 0, 1, 10),
        sent_sample(1, 100, 1, 20),
        sent_sample(2, 200, 2, 5, ok=False, error="PROJECT_BUSY"),
        sent_sample(3, 300, 2, 5, ok=False, error="HUB_BUSY", kind="write"),
        sent_sample(4, 400, 2, 5, ok=False, error="PROJECT_BUSY"),
        sent_sample(5, 500, 3, 900, ok=False, error="ReadTimeout"),
        sent_sample(6, 600, 3, 900, ok=False, error="TimeoutError"),
        dict(sent_sample(7, 700, 4, 50, ok=False, error="HUB_BUSY"), operation_id="original"),
        dict(
            sent_sample(8, 800, 4, 5, ok=False, error="other"),
            admission_rejection={"admitted": False},
        ),
        {
            "sequence": 9,
            "phase": "steady",
            "kind": "read",
            "scheduled_offset_ms": 900,
            "ok": False,
            "error": "client_in_flight_limit",
            "client_dropped": True,
        },
        {
            "sequence": 10,
            "phase": "steady",
            "kind": "read",
            "scheduled_offset_ms": 1000,
            "ok": False,
            "error": None,
            "not_sent": "admission_stopped",
        },
    ]
    summary = benchmark.summarize_open_loop(samples, arrival_rate=10, burst=0, max_in_flight=4)
    assert {k: summary[k] for k in benchmark.OPEN_LOOP_OUTCOMES} == {
        "succeeded": 2,
        "rejected": 4,
        "timeout": 2,
        "cancelled": 0,
        "failed": 1,
        "client_dropped": 1,
        "not_sent_after_stop": 1,
    }
    assert summary["rejected_by_code"] == {"HUB_BUSY": 1, "PROJECT_BUSY": 2, "other": 1}
    assert summary["scheduled"] == summary["offered_operations"] == 11
    assert summary["sent"] == 9 and summary["errors"] == 9
    assert summary["outcome_rates"]["rejected"] == 4 / 11
    assert summary["latency_ms_from_scheduled_success"] == {"p50": 11, "p95": 21, "p99": 21}
    assert summary["latency_ms_from_scheduled_all"]["p99"] == 903
    assert summary["send_lag_ms_max"] == 4
    assert summary["oldest_backlog_age_ms_max"] == 903
    assert summary["duration_seconds"] == pytest.approx(1.503)
    assert set(summary["by_kind"]) == {"read", "write"}
    assert summary["by_kind"]["write"]["rejected_by_code"] == {"HUB_BUSY": 1}
    assert summary["burst_drain"] is None
    json.dumps(summary)


@pytest.mark.asyncio
async def test_initial_failure_stops_other_reserved_but_unsent_burst_requests():
    clock = VirtualClock()
    abort = asyncio.Event()
    calls = []

    async def run(record, lane):
        calls.append(record["sequence"])
        abort.set()
        await clock.sleep(0.02)
        record.update(error="ReadTimeout", unresolved=True)

    records = await drive(
        clock, benchmark.arrival_schedule(1, operations=5, burst=3), 4, run, abort=abort
    )
    assert calls == [0]
    assert benchmark.burst_drain(records)["completed"] == 0
    summary = benchmark.summarize_open_loop(records, arrival_rate=1, burst=3, max_in_flight=4)
    assert summary["timeout"] == 1 and summary["not_sent_after_stop"] == 4
    assert summary["duration_seconds"] == pytest.approx(0.02)


def test_stage_samples_and_rate_use_the_requested_arrival_window():
    samples = [sent_sample(i, i * 250, 0, 10) for i in range(3)]
    samples[0].update(hub_first_dispatch_ms=4, agent_queue_ms=3)
    samples[1].update(hub_first_dispatch_unavailable="missing_hub_received")
    samples[2].update(ok=False, state="cancelled", error="CANCELLED", hub_first_dispatch_ms=80)
    summary = benchmark.summarize_open_loop(
        samples, arrival_rate=4, burst=0, max_in_flight=3, arrival_duration=0.7
    )
    assert summary["duration_seconds"] == 0.7
    assert summary["throughput_success_per_second"] == 2 / 0.7
    assert summary["offered_rate_mean_per_second"] == 3 / 0.7
    assert summary["cancelled"] == 1
    stages = summary["by_kind"]["read"]["stage_ms_success"]
    assert stages["hub_first_dispatch_ms"]["p95"] == 4
    assert stages["hub_first_dispatch_ms_samples"] == 1
    assert stages["hub_first_dispatch_ms_unavailable"] == {"missing_hub_received": 1}
    assert stages["agent_queue_ms_samples"] == 1


def test_backlog_age_counts_from_planned_arrival():
    samples = [sent_sample(0, 0, 50, 500), sent_sample(1, 100, 0, 10)]
    assert benchmark.backlog_at(samples, 300) == {"in_flight": 1, "oldest_age_ms": 300}
    assert benchmark.backlog_at(samples, 40) == {"in_flight": 0, "oldest_age_ms": None}
    assert benchmark.backlog_at(samples, 105) == {"in_flight": 2, "oldest_age_ms": 105}


def test_workload_mix_reuses_closed_loop_kinds():
    assert [benchmark.open_loop_kind("read", i) for i in range(8)] == ["read"] * 8
    kinds = [benchmark.open_loop_kind("read_write", i) for i in range(8)]
    assert kinds == ["write", "read", "read", "read"] * 2
    kinds = [benchmark.open_loop_kind("long_short", i) for i in range(64)]
    assert [i for i, k in enumerate(kinds) if k == "long"] == [0, 32]
    # Closed-loop rule is unchanged: every eighth worker, every fourth step.
    long = {
        (w, s)
        for w in range(16)
        for s in range(8)
        if benchmark.closed_loop_kind("long_short", w, s) == "long"
    }
    assert long == {(w, s) for w in (0, 8) for s in (0, 4)}
    assert benchmark.closed_loop_kind("read_write", 3, 4) == "write"
    assert benchmark.closed_loop_kind("read_write", 3, 5) == "read"


def test_operation_requests_match_existing_tools():
    lane = benchmark.FileLane(2)
    assert lane.path == "bench/worker-2.txt" and lane.current_sha == INITIAL_SHA
    assert benchmark.operation_request("read", lane.path, "k", None, INITIAL_SHA) == (
        "read",
        {"project": "ProjectAlpha", "path": lane.path, "idempotency_key": "k"},
    )
    name, args = benchmark.operation_request("write", lane.path, "k", "x\n", INITIAL_SHA)
    assert name == "write" and args["expected_sha256"] == INITIAL_SHA and args["content"] == "x\n"
    name, args = benchmark.operation_request("long", lane.path, "k", None, INITIAL_SHA)
    assert name == "exec" and args["task"] == "bench_long" and "path" not in args


@pytest.mark.asyncio
async def test_measure_write_updates_lane_and_rejection_is_classified(monkeypatch):
    content = "lane 0 op 0\n"
    written = hashlib.sha256(content.encode()).hexdigest()
    replies = iter(
        [
            {
                "operation_id": "w",
                "state": "succeeded",
                "pending": False,
                "result": {"ok": True, "data": {"sha256": written}},
            },
            {
                "error": {
                    "code": "PROJECT_BUSY",
                    "queue_scope": "project",
                    "admitted": False,
                    "retry_after_seconds": 2,
                    "message": "SENSITIVE",
                }
            },
        ]
    )
    seen = []

    async def rpc(client, name, args):
        seen.append((name, args))
        reply = next(replies)
        return reply, "error" in reply

    monkeypatch.setattr(benchmark, "rpc", rpc)
    lane, abort = benchmark.FileLane(0), asyncio.Event()
    write = {"ok": False, "error": None, "polls": 0}
    await benchmark.measure_operation(None, None, lane, "write", content, write, 10, abort)
    assert write["ok"] and lane.current_sha == written
    assert seen[0][1]["expected_sha256"] == INITIAL_SHA
    read = {"ok": False, "error": None, "polls": 0}
    await benchmark.measure_operation(None, None, lane, "read", None, read, 10, abort)
    assert not read["ok"] and read["error"] == "PROJECT_BUSY"
    assert read["admission_rejection"] == {
        "queue_scope": "project",
        "retry_after_seconds": 2,
        "admitted": False,
    }
    assert benchmark.open_loop_outcome(read) == "rejected"
    assert not abort.is_set() and "SENSITIVE" not in json.dumps(read)


@pytest.mark.asyncio
async def test_measure_http_failure_stops_admission_and_is_a_timeout(monkeypatch):
    async def rpc(client, name, args):
        raise httpx.ReadTimeout("SENSITIVE bearer")

    def lookup(database, key):
        raise ValueError("original_operation_missing_or_ambiguous")

    monkeypatch.setattr(benchmark, "rpc", rpc)
    monkeypatch.setattr(benchmark, "lookup_original", lookup)
    sample, abort = {"ok": False, "error": None, "polls": 0}, asyncio.Event()
    await benchmark.measure_operation(
        None, None, benchmark.FileLane(0), "read", None, sample, 10, abort
    )
    assert abort.is_set() and sample["unresolved"] and sample["error"] == "ReadTimeout"
    assert sample["recovery_error"] == "ValueError"
    assert benchmark.open_loop_outcome(sample) == "timeout"
    assert "SENSITIVE" not in json.dumps(sample)


@pytest.mark.asyncio
async def test_open_loop_workload_reports_separate_mode(monkeypatch, tmp_path):
    async def rpc(client, name, args):
        assert name == "read"
        return {
            "operation_id": args["idempotency_key"],
            "state": "succeeded",
            "pending": False,
            "result": {"ok": True, "data": {"sha256": INITIAL_SHA}},
        }, False

    async def monitor(stack, stop, rows):
        rows.append({"complete": True})
        await stop.wait()

    monkeypatch.setattr(benchmark, "rpc", rpc)
    monkeypatch.setattr(benchmark, "resource_samples", monitor)
    monkeypatch.setattr(benchmark, "enrich", lambda *args: None)
    stack = SimpleNamespace(pat="synthetic", url="http://127.0.0.1:1", hubdir=tmp_path)
    summary = await benchmark.open_loop_workload(
        stack,
        tmp_path,
        workload_name="read",
        arrival_rate=1000,
        duration=None,
        operations=5,
        burst=2,
        max_in_flight=8,
        request_timeout=10,
    )
    rows = [json.loads(line) for line in (tmp_path / "samples.jsonl").read_text().splitlines()]
    assert summary["mode"] == "open_loop" and summary["workload"] == "read"
    assert summary["succeeded"] == 5 and summary["errors"] == 0
    assert summary["planned_operations"] == 5 and not summary["admission_stopped"]
    assert summary["burst_drain"]["all_completed"] and summary["resource_sampling_complete"]
    assert sorted(r["sequence"] for r in rows) == [0, 1, 2, 3, 4]
    assert all(r["latency_ms"] == pytest.approx(r["service_ms"] + r["send_lag_ms"]) for r in rows)
    assert json.loads((tmp_path / "summary.json").read_text())["mode"] == "open_loop"


def parse(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--operations-per-worker", type=int, default=4)
    parser.add_argument("--request-timeout", type=float, default=120)
    benchmark.add_open_loop_arguments(parser)
    options = parser.parse_args(argv)
    return parser, options


@pytest.mark.parametrize(
    "argv",
    [
        ["--arrival-rate", "5"],
        ["--burst", "0"],
        ["--max-in-flight", "8"],
        ["--mode", "open_loop"],
        ["--mode", "open_loop", "--arrival-rate", "5"],
        ["--mode", "open_loop", "--arrival-rate", "5", "--operations", "5", "--duration", "5"],
        ["--mode", "open_loop", "--arrival-rate", "5,5", "--operations", "5"],
        ["--mode", "open_loop", "--arrival-rate", "nan", "--operations", "5"],
        ["--mode", "open_loop", "--arrival-rate", "5", "--operations", "5", "--burst", "6"],
        [
            "--mode",
            "open_loop",
            "--arrival-rate",
            "5",
            "--operations",
            "5",
            "--max-in-flight",
            "257",
        ],
    ],
)
def test_open_loop_flags_are_validated(argv):
    parser, options = parse(argv)
    with pytest.raises(SystemExit):
        benchmark.open_loop_rates(parser, options)


def test_cases_keep_closed_loop_default_and_separate_open_loop_labels():
    parser, options = parse([])
    assert benchmark.open_loop_rates(parser, options) == []
    cases = benchmark.benchmark_cases(options, ["read", "long_short"], [8, 16], [])
    assert [(label, lanes) for label, lanes, _ in cases] == [
        ("read-8", 8),
        ("read-16", 16),
        ("long_short-8", 8),
        ("long_short-16", 16),
    ]
    assert cases[0][2].func is benchmark.workload

    argv = ["--mode", "open_loop", "--arrival-rate", "5,12.5", "--duration", "10"]
    parser, options = parse(argv + ["--burst", "20"])
    rates = benchmark.open_loop_rates(parser, options)
    assert rates == [5.0, 12.5] and options.max_in_flight == 128
    cases = benchmark.benchmark_cases(options, ["read"], [8, 16], rates)
    assert [(label, lanes) for label, lanes, _ in cases] == [
        ("read-open-5", 128),
        ("read-open-12.5", 128),
    ]
    assert cases[1][2].func is benchmark.open_loop_workload
    assert cases[1][2].keywords["burst"] == 20
