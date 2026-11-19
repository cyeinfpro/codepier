"""Regression coverage at the boundary between durable preparation and wire send."""

import json
import time
from types import SimpleNamespace

import pytest

from hub.delivery_metrics import DeliveryTiming
from hub.runtime import Connection
from tests.test_delivery_concurrency import admit, delivery  # noqa: F401 (fixture)


def connect(e, *, fail=False):
    class Socket:
        def __init__(self):
            self.sent = []

        async def send_text(self, value):
            self.sent.append(json.loads(value))
            if fail:
                raise TimeoutError("fixture write outcome unknown")

        async def close(self, **_):
            pass

    con = Connection(Socket(), SimpleNamespace(pack=json.dumps))
    con.journal_id = "fixture-journal"
    e.runtime.connections["hot"] = con
    return con


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["due", "state", "device"])
async def test_selected_row_is_rechecked_on_the_writer(delivery, change):
    e = delivery
    identifier = admit(e)
    rows = await e.store.run_read(e.runtime._delivery_candidates, ())
    assert [row["id"] for row in rows] == [identifier]
    if change == "due":
        e.store.execute(
            "UPDATE operations SET next_attempt=? WHERE id=?", (time.time() + 30, identifier)
        )
    elif change == "state":
        e.store.execute("UPDATE operations SET state='failed' WHERE id=?", (identifier,))
    else:
        e.store.execute("UPDATE operations SET device_id='cold' WHERE id=?", (identifier,))
    assert await e.runtime._prepare_for_send(identifier, device_id=rows[0]["device_id"]) is None
    assert e.store.one("SELECT attempts FROM operations WHERE id=?", (identifier,))["attempts"] == 0
    if change == "due":
        # A direct delivery of an explicit ID may still force reconciliation.
        assert (await e.runtime._prepare_for_send(identifier))[2][0]["type"] == "call"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "payload", "deadline", "journal", "attempt", "cancel"])
async def test_retained_payload_and_current_operation_bind_wire_send(delivery, monkeypatch, change):
    e = delivery
    con = connect(e)
    identifier = admit(e)
    payload = e.store.one("SELECT payload FROM operations WHERE id=?", (identifier,))["payload"]
    decrypt = e.store.decrypt_bytes
    payload_decrypts = []

    def observe(value):
        if value == payload:
            payload_decrypts.append(value)
        return decrypt(value)

    monkeypatch.setattr(e.store, "decrypt_bytes", observe)
    prepared = await e.runtime._prepare_for_send(identifier, device_id="hot")
    if change == "payload":
        request = json.loads(decrypt(payload))
        request["args"]["path"] = "other.txt"
        e.store.execute(
            "UPDATE operations SET payload=? WHERE id=?",
            (e.store.encrypt(json.dumps(request)), identifier),
        )
    elif change == "deadline":
        deadline = time.time() + 10
        e.store.execute("UPDATE operations SET deadline=? WHERE id=?", (deadline, identifier))
    elif change == "journal":
        e.store.execute("UPDATE operations SET journal_id='other' WHERE id=?", (identifier,))
    elif change == "attempt":
        e.store.execute("UPDATE operations SET attempts=0 WHERE id=?", (identifier,))
    elif change == "cancel":
        # Wait for durable cancellation after preparation but before the wire fence.
        outcome = await e.runtime.cancel(identifier, e.principal)
        assert outcome["cancel_requested"]

    allow = change in {"none", "deadline"}
    assert await e.runtime._send_prepared(identifier, prepared) is allow
    assert len(con.socket.sent) == int(allow)
    assert payload_decrypts == [payload]
    if change == "deadline":
        assert con.socket.sent[0]["not_after"] == deadline


@pytest.mark.asyncio
async def test_send_timeout_keeps_original_receipt_and_terminal_result(delivery):
    e = delivery
    failed = connect(e, fail=True)
    identifier = admit(e)
    timing = DeliveryTiming(identifier)
    prepared = await e.runtime._prepare_for_send(identifier, timing=timing)

    with pytest.raises(TimeoutError) as failure:
        await e.runtime._send_prepared(identifier, prepared, timing=timing)
    assert failed.unusable and "send" in timing.stages
    await e.runtime._delivery_failed(identifier, failure.value)
    row = e.store.one("SELECT * FROM operations WHERE id=?", (identifier,))
    assert row["attempts"] == 1 and row["state"] == "queued" and row["result"] is None

    replacement = connect(e)
    await e.runtime.deliver(identifier)
    assert replacement.socket.sent[0]["id"] == failed.socket.sent[0]["id"] == identifier
    e.runtime.complete(row, {"ok": True, "data": {"content": "fixture"}})
    await e.runtime._delivery_failed(identifier, failure.value)  # Late failure from the old sender.
    terminal = e.store.one("SELECT state,transport_error FROM operations WHERE id=?", (identifier,))
    assert terminal == {"state": "succeeded", "transport_error": None}
