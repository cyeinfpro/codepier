"""New terminals never silently downgrade to direct even for legacy auto."""

import pytest

from agent.ssh_terminal_manager import TerminalSessionManager
from hub.ssh_terminal.models import CreateSession
from hub.ssh_terminal.service import TerminalService
from tests.test_ssh_terminal_agent import (
    FakeTmuxTransport,
    admitted_create,
    create_request,
    settled,
)


def test_hub_schema_default_and_legacy_auto_dispatch_are_durable():
    fields = {"vps_id": "fixture", "request_id": "1" * 32}
    assert CreateSession(**fields).mode == "tmux"
    # Retain explicit older request identity/fingerprint but never ask an older
    # Agent to interpret auto (older agents used to downgrade without consent).
    assert CreateSession(**fields, mode="auto").mode == "auto"
    assert TerminalService._runtime_create_mode("auto") == "tmux"
    assert TerminalService._runtime_create_mode("direct") == "direct"
    assert TerminalService._runtime_create_mode("direct", recover=True) == "tmux"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, "auto", "tmux", "direct"])
async def test_agent_requires_tmux_unless_direct_was_explicit(requested):
    class NoTmux(FakeTmuxTransport):
        async def run_control(self, command, **kwargs):
            if "tmux" in command and not hasattr(self, "command"):
                return 127, b""
            raise AssertionError("No tmux command should start remotely")

    transport = NoTmux({})
    manager = TerminalSessionManager(
        emit=lambda _: None,
        connector=lambda: transport,
        authorize=lambda _: None,
    )
    try:
        payload = create_request(manager, mode=requested)
        if requested is None:
            payload.pop("mode")
        await admitted_create(manager, payload)
        session = await settled(manager, payload["session_id"])
        if requested == "direct":
            assert session.state == "ready" and session.mode == "direct"
            assert transport.command is None
        else:
            assert session.state == "failed" and session.reason == "PERSISTENCE_UNAVAILABLE"
            assert not hasattr(transport, "command")
    finally:
        await manager.close_all()
