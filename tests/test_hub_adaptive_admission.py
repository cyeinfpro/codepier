"""Durable Hub limits and authenticated resource reports; no model CLI calls."""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.scheduler import Pressure, ProjectScheduler
from hub.admission_policy import (
    AdmissionWindow, Budget, GLOBAL_BYTES, MAX_GLOBAL, NODE_BYTES, PROJECT_BYTES,
    check_admission, resource_report, usage,
)
from hub.runtime import Runtime
from hub.scheduler_settings import revision
from shared.util import DevError
from tests.test_reliability import Peer, runtime
from tests.test_scheduler_queue_admission import project, submit

REVISION = revision({})


@pytest.mark.parametrize("limit,reason,expected", [
    (154, "adaptive", True), (129, "adaptive", True), (128, "adaptive", False),
    (32, "legacy", False), (154, "unavailable", False), (154, "pressure", False),
])
def test_real_burst_prerequisite_uses_required_peak_not_host_size(limit, reason, expected):
    from tests.test_hub_adaptive_admission_stack import burst_capacity_ready

    assert burst_capacity_ready({"project_pending_limit": limit, "reason": reason}) is expected


def test_real_burst_probe_cadence_bounds_control_requests():
    from tests.test_hub_adaptive_admission_stack import admission_probe_due

    previous, probes = -float("inf"), []
    for tick in range(1000):
        now = tick / 10
        if admission_probe_due(now, previous):
            previous = now
            probes.append(now)
    assert probes == list(range(0, 100, 3))
    assert len(probes) == 34


def test_real_burst_cpu_diagnostics_only_returns_owned_numeric_roles(monkeypatch):
    from types import SimpleNamespace
    from tests import test_hub_adaptive_admission_stack as case

    monkeypatch.setattr(case.os, "getpid", lambda: 101)
    captured = []

    def output(argv, **kwargs):
        captured.append((argv, kwargs))
        return "101 7.5\n102 20.0\n103 1.0\n999 99.0\n"

    monkeypatch.setattr(case.subprocess, "check_output", output)
    stack = SimpleNamespace(hub=SimpleNamespace(pid=102), agent=SimpleNamespace(pid=103))
    assert case.owned_cpu_percent(stack) == {"test": 7.5, "hub": 20.0, "agent": 1.0}
    assert captured == [(["ps", "-p", "101,102,103", "-o", "pid=,pcpu="], {"text": True, "timeout": 1})]


def snapshot(capacity=12, reason="healthy"):
    return {"scope": "node", "reason": reason, "running": 0, "queued": 0,
            "lanes": {lane: {"running": 0, "queued": 0, "capacity": capacity}
                      for lane in ("execution", "read", "remote")}}


def telemetry(at=0, **changes):
    return {"version": 1, "sample": at, "age_seconds": 0, "source": "linux_os_counters",
            "cpu_busy": .2, "memory_available": 8 * 1024**3, "memory_total": 16 * 1024**3,
            "io_stall": .01, **changes}


def warmed(capacity=12, end=24):
    state = AdmissionWindow()
    for at in (end - 24, end - 12, end):
        state.observe(snapshot(capacity), telemetry(at), REVISION, REVISION, at)
    return state


def install(r, capacity=12):
    peer = Peer()
    peer.scheduler_protocol = 1
    peer.admission = warmed(capacity, time.monotonic())
    r.connections["dev"] = peer
    return peer


def counts(**changes):
    return dict.fromkeys(("global_count", "global_bytes", "node_count", "node_bytes",
                          "project_count", "project_bytes", "node_reads", "project_reads", "controls"), 0) | changes


def test_growth_is_waiting_budget_not_execution_capacity():
    state = warmed()
    budget = state.current(24, REVISION)
    assert (budget.node, budget.project, budget.reserve, budget.read_reserve) == (288, 256, 32, 8)
    assert state.current(69, REVISION) == budget
    assert state.current(69.01, REVISION).node == 64
    assert state.current(24, "0" * 64).project == 32
    assert warmed(64).current(24, REVISION).node == 512
    assert warmed(1).current(24, REVISION).node == 64


@pytest.mark.parametrize("paused", [False, True])
def test_sample_age_plus_receipt_gap_resets_consecutive_health(paused):
    state = AdmissionWindow()
    state.paused = paused
    for at in (0, 12):
        state.observe(snapshot(), telemetry(at, age_seconds=15), REVISION, REVISION, at)
    assert state.current(52, REVISION).node == 64
    state.observe(snapshot(), telemetry(52), REVISION, REVISION, 52)
    assert state.current(52, REVISION).node == 64
    assert state.current(52, REVISION).paused is paused
    assert state.healthy == 1


def test_replayed_sample_cannot_grow_or_refresh_freshness():
    state = warmed()
    for now in (30, 40, 50, 60):
        state.observe(snapshot(), telemetry(24), REVISION, REVISION, now)
    assert state.received == 24
    assert state.current(70, REVISION).node == 64
    state.observe(snapshot(), telemetry(23), REVISION, REVISION, 71)
    assert state.current(71, REVISION).node == 64


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": 2}, {"source": "client"}, {"source": []},
    {"age_seconds": 16}, {"age_seconds": -1}, {"age_seconds": float("nan")},
    {"sample": float("inf")}, {"sample": 2**2048}, {"age_seconds": 2**2048},
    {"cpu_busy": 2**2048}, {"cpu_busy": True}, {"cpu_busy": -1},
    {"io_stall": 2}, {"memory_total": True}, {"memory_total": 0},
    {"memory_available": 2**70}, {"memory_total": 2**70},
])
def test_invalid_untrusted_telemetry_fails_closed(change):
    assert resource_report(telemetry(**change)) is None


@pytest.mark.parametrize("invalid", [None, {}, [], {"version": 1}])
def test_missing_old_telemetry_falls_back(invalid):
    state = warmed()
    state.observe(snapshot(), invalid, REVISION, REVISION, 36)
    assert state.current(36, REVISION).node == 64


def test_bad_snapshot_counts_and_config_revision_cannot_expand():
    for current, rev in ((snapshot() | {"running": 1}, REVISION),
                         (snapshot() | {"scope": "project"}, REVISION), (snapshot(), "0" * 64)):
        state = warmed()
        state.observe(current, telemetry(36), rev, REVISION, 36)
        assert state.current(36, REVISION).project == 32


@pytest.mark.parametrize("reason", ["starting", "metrics_unavailable", "future_unknown_reason"])
def test_unavailable_agent_reason_cannot_expand_on_conflicting_metrics(reason):
    state = warmed()
    state.observe(snapshot(reason=reason), telemetry(36), REVISION, REVISION, 36)
    assert state.current(36, REVISION).node == 64


def test_resource_pressure_shrink_and_latched_recovery():
    state = warmed()
    state.observe(snapshot(reason="resource_pressure"), telemetry(36, cpu_busy=.99), REVISION, REVISION, 36)
    assert state.current(36, REVISION).node == 64
    assert state.current(36, REVISION).retry_after == 5
    state.observe(snapshot(reason="memory_critical"), telemetry(48, memory_available=1, cpu_busy=None),
                  REVISION, REVISION, 48)
    assert state.current(48, REVISION).paused
    state.observe(snapshot(), None, REVISION, REVISION, 60)
    assert state.current(60, REVISION).paused
    for at in (72, 84):
        state.observe(snapshot(), telemetry(at), REVISION, REVISION, at)
        assert state.current(at, REVISION).paused
    state.observe(snapshot(), telemetry(96), REVISION, REVISION, 96)
    assert not state.current(96, REVISION).paused
    assert state.current(96, REVISION).node == 64
    for at in (108, 120, 132):
        state.observe(snapshot(), telemetry(at), REVISION, REVISION, at)
    assert state.current(132, REVISION).node == 288


def test_safe_recovery_samples_are_not_counted_as_expansion_samples():
    state = warmed()
    total = 4 * 1024**3
    state.observe(snapshot(reason="memory_critical"),
                  telemetry(36, memory_available=1, memory_total=total), REVISION, REVISION, 36)
    for at in (48, 60):
        state.observe(snapshot(reason="recovering"),
                      telemetry(at, cpu_busy=.8, memory_available=700 * 1024**2, memory_total=total),
                      REVISION, REVISION, at)
    state.observe(snapshot(), telemetry(72, memory_available=2 * 1024**3, memory_total=total),
                  REVISION, REVISION, 72)
    assert not state.current(72, REVISION).paused
    assert state.current(72, REVISION).node == 64
    assert state.healthy == 0


def test_small_cgroup_exits_emergency_without_claiming_expansion_capacity():
    now = [0.]
    agent = ProjectScheduler(clock=lambda: now[0])
    state = AdmissionWindow()
    total = 512 * 1024**2
    agent.capacity.update(Pressure(0., .2, 1, total, None, "linux_os_counters"))
    state.observe(agent.snapshot(), agent.admission_snapshot(), REVISION, REVISION, now[0])
    assert state.current(0, REVISION).paused
    for at in (6., 12., 18., 30., 42.):
        now[0] = at
        agent.capacity.update(Pressure(at, .2, 128 * 1024**2, total, None, "linux_os_counters"))
        state.observe(agent.snapshot(), agent.admission_snapshot(), REVISION, REVISION, at)
    assert not agent.capacity.memory_paused
    budget = state.current(now[0], REVISION)
    assert not budget.paused
    assert budget.node == 64 and budget.project == 32 and budget.reason == "recovering"
    check_admission(budget, counts(), "exec", 1)


def test_sample_age_counts_toward_hub_expiry_and_future_clock_fails_closed():
    state = warmed()
    state.observe(snapshot(), telemetry(36, age_seconds=14), REVISION, REVISION, 36)
    assert state.current(67, REVISION).node == 288
    assert state.current(67.1, REVISION).node == 64
    assert state.current(35, REVISION).node == 64


def test_agent_only_exports_fresh_resource_counters_to_heartbeat():
    now = [1.]
    scheduler = ProjectScheduler(clock=lambda: now[0])
    assert scheduler.admission_snapshot() is None
    scheduler.capacity.update(Pressure(1., .2, 8 * 1024**3, 16 * 1024**3, None, "linux_os_counters"))
    report = scheduler.admission_snapshot()
    assert resource_report(report) is not None
    assert "memory_total" not in json.dumps(scheduler.snapshot("private-project"))
    now[0] = 17
    assert scheduler.admission_snapshot() is None


@pytest.mark.parametrize(("values", "scope"), [
    ({"global_count": MAX_GLOBAL}, "hub"),
    ({"global_bytes": GLOBAL_BYTES}, "hub"),
    ({"node_count": 512}, "node"),
    ({"project_count": 256}, "project"),
    ({"node_bytes": NODE_BYTES}, "node"),
    ({"project_bytes": PROJECT_BYTES}, "project"),
])
def test_absolute_limits_apply_to_reads_and_controls(values, scope):
    for tool in ("fs_read", "exec", "integration_control"):
        with pytest.raises(DevError) as error:
            check_admission(AdmissionWindow.expanded(512), counts(**values), tool, 1)
        assert error.value.details["queue_scope"] == scope
        assert error.value.details["admitted"] is False
        assert error.value.details["retry_after_seconds"] > 0
        assert not any(key in error.value.details for key in ("payload", "global_bytes", "project_bytes"))


def test_control_exception_is_bounded_and_memory_pause_leaves_read_path():
    with pytest.raises(DevError):
        check_admission(Budget(), counts(controls=8), "integration_control", 1)
    check_admission(Budget(paused=True), counts(), "fs_read", 1)
    with pytest.raises(DevError) as error:
        check_admission(Budget(paused=True), counts(), "exec", 1)
    assert error.value.details["retry_after_seconds"] == 15


@pytest.mark.asyncio
async def test_authenticated_fresh_burst_accepts_128_deduplicates_and_counts_once(runtime):
    r, principal = runtime
    install(r)
    results = await asyncio.gather(*(submit(r, principal, "proj", i % 128) for i in range(256)))
    assert len({item["operation_id"] for item in results}) == 128
    assert all(item["pending"] for item in results)
    assert usage(r.store, "dev", "proj")["project_count"] == 128


def test_true_threaded_admission_cannot_oversell_or_duplicate(runtime):
    r, principal = runtime
    install(r)
    mapped = r.store.one("SELECT * FROM projects WHERE id='proj'")
    def admit(i):
        try:
            return r._admit_operation("fs_read", {"path": f"{i}.txt", "idempotency_key": f"atomic-{i}"},
                                      mapped, principal)[0]
        except DevError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=16) as pool:
        result = list(pool.map(admit, list(range(300)) * 2))
    rows = r.store.all("SELECT id,idem FROM operations")
    assert len(rows) == 256
    assert len({row["id"] for row in rows}) == len({row["idem"] for row in rows}) == 256
    assert result.count("PROJECT_BUSY") == 88


@pytest.mark.asyncio
async def test_heavy_burst_retains_read_headroom_and_other_project(runtime):
    r, principal = runtime
    install(r)
    project(r.store, "other")
    mapped = r.store.one("SELECT * FROM projects WHERE id='proj'")
    for i in range(248):
        r._admit_operation("exec", {"target": "agent", "command": "fixture",
                                  "idempotency_key": f"heavy-job-{i}"}, mapped, principal)
    with pytest.raises(DevError) as error:
        r._admit_operation("exec", {"target": "agent", "command": "fixture",
                                   "idempotency_key": "heavy-over"}, mapped, principal)
    assert error.value.code == "PROJECT_BUSY"
    for i in range(8):
        assert (await submit(r, principal, "proj", i))["pending"]
    assert (await submit(r, principal, "other", 0))["pending"]
    assert usage(r.store, "dev", "proj")["project_count"] == 256


@pytest.mark.asyncio
async def test_new_project_reserve_cannot_be_repeatedly_claimed_by_same_project(runtime):
    r, principal = runtime
    install(r)
    project(r.store, "other")
    for i in range(256):
        await submit(r, principal, "proj", i)
    assert (await submit(r, principal, "other", 0))["pending"]
    with pytest.raises(DevError) as error:
        await submit(r, principal, "other", 1)
    assert error.value.details["queue_reason"] == "project_fairness"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stale", "old", "disconnect", "revision", "forged_info"])
async def test_fallback_preserves_over_64_durable_receipts_and_same_key(runtime, mode):
    r, principal = runtime
    peer = install(r)
    receipts = [await submit(r, principal, "proj", i) for i in range(128)]
    if mode == "stale":
        peer.admission.received -= 60
    elif mode == "old":
        peer.scheduler_protocol = 0
    elif mode == "disconnect":
        r.connections.clear()
    elif mode == "revision":
        r.store.execute("INSERT INTO meta(key,value) VALUES ('device_scheduler:dev',?)", ('{"maximum":4}',))
    else:
        r.connections.clear()
        r.store.execute("UPDATE devices SET info=? WHERE id='dev'",
                        (json.dumps({"scheduler": snapshot(64), "scheduler_revision": REVISION, "admission": telemetry()}),))
    with pytest.raises(DevError):
        await submit(r, principal, "proj", 128)
    assert (await submit(r, principal, "proj", 0))["operation_id"] == receipts[0]["operation_id"]
    assert usage(r.store, "dev", "proj")["project_count"] == 128
    assert all(row["state"] == "queued" for row in r.store.all("SELECT state FROM operations"))


@pytest.mark.asyncio
async def test_runtime_restart_recounts_durable_queue_and_keeps_cancel_access(runtime):
    r, principal = runtime
    install(r)
    receipts = [await submit(r, principal, "proj", i) for i in range(128)]
    restarted = Runtime(r.store)
    restarted.wait_seconds = 0
    assert usage(restarted.store, "dev", "proj")["project_count"] == 128
    assert (await submit(restarted, principal, "proj", 0))["operation_id"] == receipts[0]["operation_id"]
    assert restarted.operation_row(receipts[0]["operation_id"], principal, status_only=True)
    with pytest.raises(DevError):
        await submit(restarted, principal, "proj", 128)
    # Existing authorization/final-send recovery code handles this cancellation.
    identifier = receipts[0]["operation_id"]
    r.store.execute("UPDATE operations SET cancel_requested=1 WHERE id=?", (identifier,))
    await restarted.deliver(identifier)
    assert r.store.one("SELECT state FROM operations WHERE id=?", (identifier,))["state"] == "cancelled"


@pytest.mark.asyncio
async def test_expanded_queue_revocation_prevents_dispatch(runtime):
    r, principal = runtime
    peer = install(r)
    receipt = await submit(r, principal, "proj", 0)
    r.store.execute("UPDATE projects SET root='/tmp/remapped' WHERE id='proj'")
    await r.deliver(receipt["operation_id"])
    row = r.store.one("SELECT * FROM operations WHERE id=?", (receipt["operation_id"],))
    assert json.loads(row["result"])["error"]["code"] == "AUTHORIZATION_CHANGED"
    assert peer.packets == []


def test_heartbeat_requires_current_authorized_connection_and_actual_fields(runtime):
    r, _ = runtime
    peer = install(r)
    before = peer.admission.received
    replaced = Peer()
    replaced.scheduler_protocol = 1
    r._heartbeat("dev", replaced, {}, {"scheduler": snapshot(64), "scheduler_revision": REVISION, "admission": telemetry(time.monotonic())})
    assert peer.admission.received == before
    r._heartbeat("dev", peer, {}, {"scheduler": snapshot()})
    assert r._admission_budget("dev").node == 64
    r.store.execute("UPDATE devices SET enabled=0 WHERE id='dev'")
    r._heartbeat("dev", peer, {}, {"scheduler": snapshot(64), "scheduler_revision": REVISION, "admission": telemetry(time.monotonic())})
    assert r._admission_budget("dev").node == 64


def test_all_active_states_and_only_active_states_consume_durable_budget(runtime):
    r, principal = runtime
    mapped = r.store.one("SELECT * FROM projects WHERE id='proj'")
    for i, state in enumerate(("queued", "running", "reconnecting", "cancelling", "succeeded", "failed")):
        identifier, _ = r._admit_operation("fs_read", {"path": f"{i}.txt", "idempotency_key": f"states-{i}"}, mapped, principal)
        r.store.execute("UPDATE operations SET state=? WHERE id=?", (state, identifier))
    summary = usage(r.store, "dev", "proj")
    assert summary["project_count"] == summary["global_count"] == 4
    assert summary["project_bytes"] > 0


def test_multiple_sqlite_connections_share_atomic_quota_and_idempotency(runtime):
    from hub.store import Store
    r, principal = runtime
    other_store = Store(r.store.directory)
    try:
        other = Runtime(other_store)
        mapped = r.store.one("SELECT * FROM projects WHERE id='proj'")
        def admit(item):
            store_index, value = item
            current = (r, other)[store_index]
            try:
                return current._admit_operation("fs_read",
                    {"path": f"{value}.txt", "idempotency_key": f"sqlite-atomic-{value}"}, mapped, principal)[0]
            except DevError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=12) as pool:
            result = list(pool.map(admit, [(store_index, value) for value in range(48) for store_index in range(2)]))
        assert all(result[i] == result[i + 1] for i in range(0, len(result), 2))
        assert len(set(result) - {"PROJECT_BUSY"}) == 32
        assert result.count("PROJECT_BUSY") == 32
        assert usage(r.store, "dev", "proj")["project_count"] == 32
        assert len(r.store.all("SELECT DISTINCT idem FROM operations")) == 32
    finally:
        other_store.close()


def test_multiple_connections_enforce_actual_ciphertext_byte_limit(runtime, monkeypatch):
    import hub.admission_policy as policy
    from hub.store import Store
    r, principal = runtime
    mapped = r.store.one("SELECT * FROM projects WHERE id='proj'")
    def arguments(value):
        return {"path": f"{value:02}.txt", "idempotency_key": f"byte-race-key-{value:02}"}
    first, _ = r._admit_operation("fs_read", arguments(0), mapped, principal)
    payload = r.store.one("SELECT payload FROM operations WHERE id=?", (first,))["payload"]
    size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
    assert usage(r.store, "dev", "proj")["project_bytes"] == size
    monkeypatch.setattr(policy, "PROJECT_BYTES", 4 * size)
    other_store = Store(r.store.directory)
    try:
        other = Runtime(other_store)
        def admit(item):
            store_index, value = item
            try:
                return (r, other)[store_index]._admit_operation("fs_read", arguments(value), mapped, principal)[0]
            except DevError as error:
                assert error.details["queue_reason"] == "admission_bytes"
                return error.code
        with ThreadPoolExecutor(max_workers=12) as pool:
            result = list(pool.map(admit, [(store_index, value) for value in range(16) for store_index in range(2)]))
        assert all(result[i] == result[i + 1] for i in range(0, len(result), 2))
        assert len(set(result) - {"PROJECT_BUSY"}) == 4
        summary = usage(r.store, "dev", "proj")
        assert summary["project_count"] == 4
        assert summary["project_bytes"] == 4 * size
        assert summary["project_bytes"] == sum(len(row["payload"].encode("utf-8")) for row in r.store.all("SELECT payload FROM operations"))
    finally:
        other_store.close()


@pytest.mark.parametrize("paused", [False, True])
def test_overload_read_reserve_survives_preexisting_heavy_backlog(paused):
    budget = Budget(reason="pressure", paused=paused, read_reserve=8)
    summary = counts(global_count=128, node_count=128, project_count=128)
    for i in range(7):
        check_admission(budget, summary, "fs_read", 1)
        summary["global_count"] += 1
        summary["node_count"] += 1
        summary["project_count"] += 1
        summary["node_reads"] += 1
        summary["project_reads"] += 1
    with pytest.raises(DevError):
        check_admission(budget, summary, "fs_read", 1)
    other_project = summary | {"project_count": 0, "project_reads": 0, "project_bytes": 0}
    check_admission(budget, other_project, "fs_read", 1)
    with pytest.raises(DevError):
        check_admission(budget, other_project | {"node_reads": 8, "project_reads": 1}, "fs_read", 1)
    # A completion frees one read place, without giving A the final B reserve.
    check_admission(budget, summary | {"node_reads": 6, "project_reads": 6}, "fs_read", 1)
    with pytest.raises(DevError):
        check_admission(budget, summary, "exec", 1)
    with pytest.raises(DevError):
        check_admission(Budget(), counts(global_count=128, node_count=128, project_count=128), "fs_read", 1)


def test_rejected_new_request_rolls_back_preparation_side_effects(runtime, monkeypatch):
    import hub.admission_policy as policy
    r, principal = runtime
    mapped = r.store.one("SELECT * FROM projects WHERE id='proj'")
    monkeypatch.setattr(policy, "PROJECT_BYTES", 1)
    monkeypatch.setattr(r.integrations, "prepare",
        lambda *args: r.store.execute("INSERT INTO meta(key,value) VALUES('prepare-rollback-test','fixture')"))
    with pytest.raises(DevError):
        r._admit_operation("fs_read", {"path": "a.txt", "idempotency_key": "rollback-preparation"}, mapped, principal)
    assert not r.store.one("SELECT value FROM meta WHERE key='prepare-rollback-test'")


def test_byte_rejection_rolls_back_new_operation_and_replay_still_recovers(runtime, monkeypatch):
    import hub.admission_policy as policy
    r, principal = runtime
    mapped = r.store.one("SELECT * FROM projects WHERE id='proj'")
    args = {"path": "a.txt", "idempotency_key": "byte-replay-test"}
    identifier, _ = r._admit_operation("fs_read", args, mapped, principal)
    pending = usage(r.store, "dev", "proj")
    monkeypatch.setattr(policy, "PROJECT_BYTES", pending["project_bytes"])
    assert r._admit_operation("fs_read", args, mapped, principal)[0] == identifier
    with pytest.raises(DevError) as error:
        r._admit_operation("fs_read", {"path": "private-secret.txt", "idempotency_key": "byte-new-operation"}, mapped, principal)
    assert error.value.details["queue_reason"] == "admission_bytes"
    assert "private-secret" not in str(error.value)
    assert usage(r.store, "dev", "proj")["project_count"] == 1


def test_admission_error_audit_preserves_fixed_code_without_arbitrary_details(monkeypatch):
    from hub import mcp_request_audit
    events = []
    monkeypatch.setattr(mcp_request_audit, "write_event", events.append)
    for code in ("PROJECT_BUSY", "DEVICE_BUSY", "HUB_BUSY", "private-command"):
        mcp_request_audit.Trace(True, "POST").record("tool_error", error_code=code)
    assert [row["error_code"] for row in events] == ["PROJECT_BUSY", "DEVICE_BUSY", "HUB_BUSY", "OTHER"]


def test_corrupt_config_cannot_clear_known_memory_emergency(runtime):
    r, _ = runtime
    peer = install(r)
    peer.admission.paused = True
    r.store.execute("INSERT INTO meta(key,value) VALUES ('device_scheduler:dev','invalid-json')")
    assert r._admission_budget("dev").paused


def test_wire_project_snapshot_cannot_refresh_node_admission(runtime):
    r, _ = runtime
    peer = install(r)
    r._heartbeat("dev", peer, {}, {"scheduler": snapshot() | {"scope": "project"},
        "scheduler_revision": REVISION, "admission": telemetry(time.monotonic())})
    assert r._admission_budget("dev").node == 64


def test_host_cpu_diagnostic_is_bounded_basename_only(monkeypatch):
    import tests.test_hub_adaptive_admission_stack as stack_test
    calls = []
    def ps(command, **kwargs):
        calls.append(command)
        return '90.0 /private/example/worker\n80.0 /private/example/name with spaces\n' + ''.join(
            f'{i}.0 /tmp/private/worker-{i}\n' for i in range(12))
    monkeypatch.setattr(stack_test.subprocess, 'check_output', ps)
    result = stack_test.host_cpu_leaders()
    assert calls == [['ps', '-A', '-o', 'pcpu=,comm=']]
    assert len(result) == 8
    assert result[0] == {'name': 'worker', 'cpu_percent': 90.0}
    assert result[1]['name'] == '[redacted-name]'
    assert all('/' not in row['name'] and 'private' not in row['name'] for row in result)


def test_host_cpu_diagnostic_unavailable_does_not_change_admission(monkeypatch):
    import tests.test_hub_adaptive_admission_stack as stack_test
    def failed(*args, **kwargs):
        raise OSError('unavailable')
    monkeypatch.setattr(stack_test.subprocess, 'check_output', failed)
    assert stack_test.host_cpu_leaders() is None
    assert not stack_test.burst_capacity_ready({'reason': 'pressure', 'project_pending_limit': 32})
