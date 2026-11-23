"""In-memory Agent and protocol tests for resuming the same SSH session."""

import asyncio
import uuid

import pytest
from pydantic import ValidationError

from agent.ssh_terminal_manager import TerminalSessionManager
from hub.ssh_terminal.models import AttachTicket
from hub.ssh_terminal.service import TerminalService
from shared.ssh_terminal_protocol import TerminalProtocolError, validate_control
from tests.test_ssh_terminal_agent import (
    FakeTmuxTransport,
    FakeTransport,
    admitted_create,
    attach_request,
    create_request,
    project,
    settled,
)


def test_attach_ticket_and_attached_wire_only_accept_typed_recovery_metadata():
    sid = "a" * 32
    assert AttachTicket(renderer_id=sid, resume_from_live=True).resume_from_live
    with pytest.raises(ValidationError):
        AttachTicket(renderer_id=sid, resume_from_live="true")
    payload = {
        "type": "attached",
        "attachment_id": sid,
        "session_id": sid,
        "runtime_generation": sid,
        "stream_epoch": "1",
        "writer_epoch": "2",
        "render_epoch": "2",
        "mode": "direct",
        "resume_strategy": "live",
        "state": "ready",
        "start_offset": "321",
        "history_recovery": "live_tail",
        "history_incomplete": False,
    }
    assert validate_control(payload, direction="server") == payload
    for field, bad in [("history_incomplete", "false"), ("history_recovery", "guess")]:
        with pytest.raises(TerminalProtocolError):
            validate_control({**payload, field: bad}, direction="server")
    TerminalService._verify_history_recovery(payload)
    with pytest.raises(TerminalProtocolError, match="不支持"):
        TerminalService._verify_history_recovery({"mode": "direct", "resume_strategy": "live"})


@pytest.mark.asyncio
async def test_resized_live_direct_uses_same_pty_and_only_future_output_after_consent():
    agent_events, transports = [], []

    def connector():
        transport = FakeTransport()
        transports.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=agent_events.append, connector=connector, authorize=lambda _: None
    )
    try:
        create = create_request(manager, mode="direct")
        await admitted_create(manager, create)
        session = await settled(manager, create["session_id"])
        first = attach_request(manager, create)
        await manager.control("attach", first, project())
        transport = transports[0]
        transport.on_data(b"retained-but-unreproducible")
        session.resized = True
        await manager.control("detach", first, project())
        next_attach = attach_request(manager, create, writer_epoch="2", render_epoch="2")
        with pytest.raises(TerminalProtocolError) as refused:
            await manager.control("attach", next_attach, project())
        assert refused.value.code == "HISTORY_GAP"
        base = session.ring.end_offset
        result = await manager.control(
            "attach", {**next_attach, "resume_from_live": True}, project()
        )
        assert result["session_id"] == create["session_id"]
        assert result["history_recovery"] == "live_tail"
        assert result["start_offset"] == str(base)
        assert session.transport is transport and len(transports) == 1
        assert transport.writes == []
        transport.on_data(b"future-only")
        for _ in range(6):
            await asyncio.sleep(0)
        output = [
            event
            for event in agent_events
            if event.get("writer_epoch") == "2" and event.get("payload")
        ]
        assert [item["payload"] for item in output] == [b"future-only"]
        assert output[0]["offset"] == str(base)

        with pytest.raises(TerminalProtocolError) as conflict:
            await manager.control(
                "attach",
                {
                    **next_attach,
                    "attachment_id": uuid.uuid4().hex,
                    "resume_from_live": True,
                },
                project(),
            )
        assert conflict.value.code == "STALE_WRITER"
        session.lease_until = 0
        with pytest.raises(TerminalProtocolError):
            await manager.control("attach", {**next_attach, "resume_from_live": True}, project())
        assert len(transports) == 1
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_tmux_recovery_redraws_exact_existing_identity_without_new_shell():
    remote = {}
    manager = TerminalSessionManager(
        emit=lambda _: None,
        connector=lambda: FakeTmuxTransport(remote),
        authorize=lambda _: None,
    )
    try:
        create = create_request(manager, mode="tmux")
        await admitted_create(manager, create)
        session = await settled(manager, create["session_id"])
        first = attach_request(manager, create)
        await manager.control("attach", first, project())
        await manager.control("detach", first, project())
        original = session.public()["tmux_ref"]
        second = attach_request(
            manager,
            create,
            writer_epoch="2",
            render_epoch="2",
            renderer_id=first["renderer_id"],
            resume_from_live=True,
        )
        restored = await manager.control("attach", second, project())
        assert restored["resume_strategy"] == "redraw"
        assert restored["tmux_ref"] == original
        assert restored["session_id"] == create["session_id"]
        assert remote["creates"] == 1
    finally:
        await manager.close_all()
