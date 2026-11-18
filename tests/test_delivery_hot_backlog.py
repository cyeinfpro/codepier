"""Exercise actual durable scheduling with a deterministic local transport."""

import asyncio
import json
import time

import pytest

from hub.runtime import Runtime
from hub.store import Store


@pytest.mark.asyncio
@pytest.mark.parametrize("devices", [1, 2])
@pytest.mark.parametrize("rolling", [False, True], ids=["original-batch", "rolling"])
async def test_due_control_and_cold_reader_during_hot_backlog(
    tmp_path, monkeypatch, devices, rolling
):
    initial_hot = [f"hot-{i:03}" for i in range(140)]
    injections = {f"hot-{i:03}": f"stream-{i:03}" for i in range(0, 140, 8)}
    control = {"late-cancel": "cancel", "late-probe": "probe"}
    cold = ["cold-read"] if devices == 2 else []
    expected_ids = set(initial_hot) | set(injections.values()) | set(control) | set(cold)

    monkeypatch.setenv("HUB_DELIVERY_ROLLING_DEVICES", "hot" if rolling else "")
    monkeypatch.setenv("HUB_DELIVERY_DEVICE_CHUNK", "16")
    monkeypatch.setenv("HUB_DELIVERY_PREFETCH", "2")
    monkeypatch.setenv("HUB_DELIVERY_SLOTS", "8")
    store = Store(tmp_path / "hub")
    runtime = Runtime(store)
    sent = []
    errors = []
    first_batch = []
    cold_sent = asyncio.Event()
    stamp = time.time() - 1

    def enqueue(identifier, *, device="hot", tool="read", state="queued", created=stamp, due=0):
        store.db.execute(
            """INSERT INTO operations
               (id,device_id,actor,tool,args_summary,fingerprint,state,created,updated,next_attempt)
               VALUES (?,?,'fixture',?,'{}','fixture',?,?,?,?)""",
            (identifier, device, tool, state, created, created, due),
        )

    with store.transaction():
        enqueue("late-cancel", state="cancelling", created=stamp - 1, due=stamp + 600)
        enqueue("late-probe", state="reconnecting", created=stamp - 1, due=stamp + 600)
        for i, identifier in enumerate(initial_hot):
            enqueue(identifier, tool="write" if i == 1 else "read")

    def settle_and_inject(identifier):
        with store.transaction():
            store.db.execute("UPDATE operations SET state='succeeded' WHERE id=?", (identifier,))
            if identifier == initial_hot[0]:
                store.db.execute(
                    "UPDATE operations SET next_attempt=0 WHERE id IN ('late-cancel','late-probe')"
                )
                if cold:
                    enqueue(cold[0], device="cold", created=time.time())
            if identifier in injections:
                enqueue(injections[identifier], created=time.time())

    async def prepare(identifier, *, device_id, timing):
        # Use synthetic packets so connection and Agent execution latency
        # cannot change the schedule. Candidate selection and refill use SQLite.
        packet = {"type": control.get(identifier, "call"), "id": identifier}
        return device_id, None, [packet]

    async def send(identifier, prepared, *, timing):
        _, _, packets = prepared
        sent.append(packets[0]["id"])
        await store.run(settle_and_inject, identifier)
        if identifier == initial_hot[0]:
            runtime.wake_delivery()
            if cold:
                # Simulate a stalled hot connection while a different device
                # is admitted and sends through the same global slot budget.
                await asyncio.wait_for(cold_sent.wait(), timeout=5)
        if identifier in cold:
            cold_sent.set()
        if len(sent) == len(expected_ids):
            runtime.stopping = True
            runtime.wake_delivery()
        return True

    async def failed(identifier, exc):
        errors.append((identifier, exc))
        runtime.stopping = True
        runtime.wake_delivery()

    start = runtime._start_device_deliveries

    def record_selection(active_devices, rows):
        if rows and not first_batch:
            first_batch.extend(row["id"] for row in rows)
        start(active_devices, rows)

    monkeypatch.setattr(runtime, "_prepare_for_send", prepare)
    monkeypatch.setattr(runtime, "_send_prepared", send)
    monkeypatch.setattr(runtime, "_delivery_failed", failed)
    monkeypatch.setattr(runtime, "_start_device_deliveries", record_selection)

    try:
        await asyncio.wait_for(runtime.delivery_loop(), timeout=10)
        assert not errors
        assert len(sent) == len(expected_ids) and set(sent) == expected_ids
        remaining = store.one("SELECT count(*) AS n FROM operations WHERE state!='succeeded'")
        assert remaining["n"] == 0
        assert runtime.delivery_slots._value == 8

        hot_sent = [identifier for identifier in sent if identifier not in cold]
        assert [identifier for identifier in hot_sent if identifier not in control] == [
            *initial_hot,
            *injections.values(),
        ]
        batch_size = 16 if rolling else 128
        assert first_batch == initial_hot[:batch_size]
        assert hot_sent[batch_size : batch_size + 2] == list(control)
        if cold:
            assert sent[:2] == [initial_hot[0], cold[0]]
        print(
            json.dumps(
                {
                    "devices": devices,
                    "rolling": rolling,
                    "initial_hot_batch": len(first_batch),
                    "hot_sends_before_cancel": hot_sent.index("late-cancel"),
                    "hot_sends_before_probe": hot_sent.index("late-probe"),
                    "total_unique_sends": len(sent),
                    "cold_send_position": sent.index(cold[0]) + 1 if cold else None,
                }
            )
        )
    finally:
        store.close()
