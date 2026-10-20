"""Bound HTTP request buffering without occupying long-lived operation slots."""
import asyncio

import pytest

from hub.http import BodyLimit


def scope():
    return {"type": "http", "path": "/api/fixture", "method": "POST", "headers": []}


def request(data):
    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    return receive


def collector():
    messages = []
    async def send(message):
        messages.append(message)
    return messages, send


@pytest.mark.asyncio
async def test_slow_body_slots_are_bounded_and_released_on_cancel():
    entered = asyncio.Event()
    blocked = asyncio.Event()
    called = []

    async def app(current, receive, send):
        called.append(await receive())

    async def slow_receive():
        entered.set()
        await blocked.wait()

    gate = BodyLimit(app, max_inflight=1)
    _, first_send = collector()
    first = asyncio.create_task(gate(scope(), slow_receive, first_send))
    await entered.wait()
    rejected, send = collector()
    await gate(scope(), request(b"x"), send)
    assert rejected[0]["status"] == 429 and not called
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert gate.inflight == 0 and gate.buffered_bytes == 0
    await gate(scope(), request(b"x"), first_send)
    assert called[0]["body"] == b"x"


@pytest.mark.asyncio
async def test_body_deadline_returns_408_before_business_execution():
    never = asyncio.Event()
    called = []

    async def app(*args):
        called.append(True)

    async def receive():
        await never.wait()

    gate = BodyLimit(app, timeout=.01)
    messages, send = collector()
    await gate(scope(), receive, send)
    assert messages[0]["status"] == 408 and not called
    assert gate.inflight == 0 and gate.buffered_bytes == 0


@pytest.mark.asyncio
async def test_buffered_byte_budget_survives_until_handler_finishes():
    handling = asyncio.Event()
    finish = asyncio.Event()
    delivered = []

    async def app(current, receive, send):
        delivered.append((await receive())["body"])
        if len(delivered) == 1:
            handling.set()
            await finish.wait()

    gate = BodyLimit(app, max_inflight=1, max_buffered_bytes=4)
    _, send = collector()
    first = asyncio.create_task(gate(scope(), request(b"four"), send))
    await handling.wait()
    assert gate.inflight == 0 and gate.buffered_bytes == 4
    rejected, reject_send = collector()
    await gate(scope(), request(b"x"), reject_send)
    assert rejected[0]["status"] == 429
    assert delivered == [b"four"]
    finish.set()
    await first
    assert gate.buffered_bytes == 0
    await gate(scope(), request(b"x"), send)
    assert delivered == [b"four", b"x"]


@pytest.mark.asyncio
async def test_empty_stream_does_not_hold_body_read_slot_or_byte_budget():
    handling = asyncio.Event()
    finish = asyncio.Event()
    delivered = []

    async def app(current, receive, send):
        delivered.append((await receive())["body"])
        if len(delivered) == 1:
            handling.set()
            await finish.wait()

    gate = BodyLimit(app, max_inflight=1, max_buffered_bytes=1)
    _, send = collector()
    stream = asyncio.create_task(gate(scope(), request(b""), send))
    await handling.wait()
    await gate(scope(), request(b"x"), send)
    assert delivered == [b"", b"x"]
    finish.set()
    await stream
    assert gate.inflight == 0 and gate.buffered_bytes == 0
