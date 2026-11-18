from dataclasses import replace
import time

import pytest

from hub.delivery_candidates import candidates, refill
from hub.runtime import Runtime
from hub.store import Store
from shared.config import RuntimeConfig


def insert_operations(store, rows):
    with store.transaction():
        store.db.executemany(
            """INSERT INTO operations
               (id,device_id,actor,tool,args_summary,fingerprint,state,created,updated,next_attempt)
               VALUES(?,?,'fixture','read','{}','fixture',?,?,0,?)""",
            rows,
        )


def test_rollout_limits_only_selected_devices_and_skips_ineligible_rows(tmp_path):
    store = Store(tmp_path / "hub")
    try:
        # These aren't eligible, although their timestamps would make them first.
        insert_operations(
            store,
            [
                ("future", "hot", "queued", 1, 200),
                ("finished", "hot", "completed", 1, 0),
                ("z-cancel", "hot", "cancelling", 2, 20),
                ("a-recovery", "hot", "reconnecting", 2, 5),
                ("probe", "hot", "running", 3, 10),
                ("new", "hot", "queued", 4, 0),
            ]
            + [(f"cold-{i}", "cold", "queued", 10 + i, 0) for i in range(130)],
        )
        config = RuntimeConfig(10, 10, 1, delivery_device_chunk=2)
        assert [row["id"] for row in candidates(store, 100, (), config)][:5] == [
            "z-cancel",
            "cold-0",
            "a-recovery",
            "cold-1",
            "probe",
        ]

        rolled = replace(config, delivery_rolling_devices=("hot",))
        rows = candidates(store, 100, (), rolled)
        assert len(rows) == 128
        assert [row["id"] for row in rows if row["device_id"] == "hot"] == [
            "z-cancel",
            "a-recovery",
        ]
        assert rows[0]["due"] == 20
        assert [row["id"] for row in candidates(store, 100, ("cold",), rolled)] == [
            "z-cancel",
            "a-recovery",
        ]
        assert [row["id"] for row in refill(store, 100, "hot", ("z-cancel", "a-recovery"), 2)] == [
            "probe",
            "new",
        ]
        store.execute(
            "UPDATE operations SET state='cancelling',cancel_requested=1,next_attempt=0 WHERE id=?",
            ("a-recovery",),
        )
        assert refill(store, 100, "hot", ("z-cancel", "a-recovery"), 2) is None
        assert [row["id"] for row in candidates(store, 100, ("cold",), rolled)] == [
            "z-cancel",
            "a-recovery",
        ]

        all_devices = replace(config, delivery_rolling_devices=("*",))
        assert [row["id"] for row in candidates(store, 100, (), all_devices)] == [
            "z-cancel",
            "cold-0",
            "a-recovery",
            "cold-1",
        ]
    finally:
        store.close()


def test_additive_index_migration_preserves_due_rows_on_reopen(tmp_path):
    root = tmp_path / "hub"
    store = Store(root)
    try:
        store.execute("DROP INDEX op_delivery_due")
        store.execute("DROP INDEX op_delivery_device")
        insert_operations(store, [("receipt", "device", "reconnecting", 1, 5)])
    finally:
        store.close()

    store = Store(root)
    try:
        config = RuntimeConfig(10, 10, 1)
        assert candidates(store, 4, (), config) == []
        assert candidates(store, 5, (), config) == [
            {"id": "receipt", "device_id": "device", "due": 5.0, "attempts": 0}
        ]
        assert refill(store, 5, "device", (), 1) == [
            {"id": "receipt", "due": 5.0, "attempts": 0}
        ]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cancel_for_already_sent_row_yields_at_chunk_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_DELIVERY_ROLLING_DEVICES", "device")
    monkeypatch.setenv("HUB_DELIVERY_DEVICE_CHUNK", "3")
    monkeypatch.setenv("HUB_DELIVERY_PREFETCH", "1")
    store = Store(tmp_path / "hub")
    runtime = Runtime(store)
    sent = []

    async def prepare(identifier, *, device_id, timing):
        row = await store.run_hint_read(
            store.one, "SELECT cancel_requested FROM operations WHERE id=?", (identifier,)
        )
        action = "cancel" if row["cancel_requested"] else "call"
        return device_id, None, [{"type": action}]

    async def send(identifier, prepared, *, timing):
        action = prepared[2][0]["type"]
        sent.append((identifier, action))
        if identifier == "call-0" and action == "call":
            # User requests cancel after the call has crossed the wire.
            statement = """UPDATE operations SET state='cancelling',
                           cancel_requested=1,next_attempt=0 WHERE id=?"""
        else:
            statement = "UPDATE operations SET state='succeeded' WHERE id=?"
        await store.run(store.execute, statement, (identifier,))
        return True

    monkeypatch.setattr(runtime, "_prepare_for_send", prepare)
    monkeypatch.setattr(runtime, "_send_prepared", send)
    try:
        insert_operations(
            store, [(f"call-{i}", "device", "queued", time.time() - 1, 0) for i in range(8)]
        )

        async def deliver_next_round():
            selected = await store.run_hint_read(runtime._delivery_candidates, ())
            await runtime.deliver_device(
                [row["id"] for row in selected], device_id="device", selected=selected
            )

        await deliver_next_round()
        assert sent == [("call-0", "call"), ("call-1", "call"), ("call-2", "call")]

        await deliver_next_round()
        assert sent[3] == ("call-0", "cancel")
        assert sent[4:] == [(f"call-{i}", "call") for i in range(3, 8)]
    finally:
        store.close()
