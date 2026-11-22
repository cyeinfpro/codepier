"""Regression coverage for Agent SSH attachment and output edge cases."""

import asyncio
import uuid

import pytest

from agent.ssh_terminal_manager import TerminalSessionManager
from shared.ssh_terminal_config import TerminalConfig
from shared.ssh_terminal_protocol import TerminalProtocolError
from tests.test_ssh_terminal_agent import (
    FakeTransport,
    admitted_create,
    attach_request,
    create_request,
    project,
    settled,
)


@pytest.mark.asyncio
async def test_resize_sequence_is_scoped_to_authenticated_writer():
    transport = FakeTransport()
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: transport, authorize=lambda _: None
    )
    request = create_request(manager)
    scope = project()

    async def resize(attach, sequence, cols, rows):
        return await manager.control(
            "resize", {**attach, "resize_seq": str(sequence), "cols": cols, "rows": rows}, scope
        )

    try:
        await admitted_create(manager, request)
        await settled(manager, request["session_id"])
        original = attach_request(manager, request)
        await manager.control("attach", original, scope)
        await resize(original, 5, 140, 40)
        assert transport.size == (140, 40)
        await manager.control("detach", original, scope)
        await manager.control("attach", original, scope)
        await resize(original, 1, 100, 25)
        assert transport.size == (140, 40), (
            "a reconnect retaining authority must retain its resize sequence"
        )
        takeover = {**original, "attachment_id": uuid.uuid4().hex, "writer_epoch": "2"}
        await manager.control("takeover", takeover, scope)
        await resize(takeover, 1, 80, 24)
        assert transport.size == (80, 24), (
            "a new writer must be able to start its resize sequence at one"
        )
        await resize(takeover, 1, 180, 70)
        assert transport.size == (80, 24), "a duplicate in that writer's sequence must be ignored"
        for stale, reason in (
            (original, "STALE_WRITER"),
            ({**takeover, "stream_epoch": "2"}, "STALE_WRITER"),
            ({**takeover, "runtime_generation": uuid.uuid4().hex}, "STALE_GENERATION"),
        ):
            with pytest.raises(TerminalProtocolError) as error:
                await resize(stale, 99, 180, 70)
            assert error.value.code == reason
            assert transport.size == (80, 24)
    finally:
        await manager.close_all()


@pytest.mark.parametrize("low_water", [0, 1024])
@pytest.mark.asyncio
async def test_paused_ssh_resumes_at_inclusive_output_low_water(low_water):
    transport = FakeTransport()
    config = TerminalConfig(
        ring_bytes=65536,
        ssh_window_bytes=32768,
        frame_bytes=4096,
        output_high_water_bytes=32768,
        output_low_water_bytes=low_water,
    )
    events = []
    manager = TerminalSessionManager(
        emit=events.append, connector=lambda: transport, config=config, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attach = attach_request(manager, request)
        await manager.control("attach", attach, project())
        payload = b"x" * (config.ring_bytes - config.ssh_window_bytes)
        transport.on_data(payload)
        for _ in range(len(payload) // config.frame_bytes + 2):
            await asyncio.sleep(0)
        assert transport.paused
        assert sum(
            len(event["payload"]) for event in events if event["type"] == "terminal_output"
        ) == len(payload)
        boundary = len(payload) - low_water
        above = {**attach, "rendered_offset": str(boundary - 1)}
        assert (await manager.acknowledge(above))["accepted"]
        assert transport.paused
        invalid = {**attach, "rendered_offset": str(boundary), "attachment_id": uuid.uuid4().hex}
        assert not (await manager.acknowledge(invalid))["accepted"]
        assert transport.paused
        assert (await manager.acknowledge({**attach, "rendered_offset": str(boundary)}))["accepted"]
        assert not transport.paused
        assert session.output_paused_at is None
    finally:
        await manager.close_all()
