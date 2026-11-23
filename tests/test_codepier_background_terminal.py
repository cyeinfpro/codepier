"""Deterministic tests without SSH, existing home credentials or real VPS hosts."""

import asyncio
import uuid

import pytest

from agent.ssh_terminal_manager import TerminalSessionManager
from tests.test_ssh_terminal_agent import (
    FakeTmuxTransport,
    admitted_create,
    attach_request,
    create_request,
    project,
    settled,
)


@pytest.mark.asyncio
async def test_expired_tmux_client_cannot_overwrite_reattach_to_same_remote_identity():
    clock, remote = [0.0], {}
    closing, release = asyncio.Event(), asyncio.Event()

    class DelayedChannel(FakeTmuxTransport):
        async def close_channel(self):
            self.channel_closed = True
            closing.set()
            await release.wait()

    transport = DelayedChannel(remote)
    manager = TerminalSessionManager(
        emit=lambda _: None,
        authorize=lambda _: None,
        connector=lambda: transport,
        clock=lambda: clock[0],
    )
    pending = []
    try:
        create = create_request(manager, mode="tmux")
        await admitted_create(manager, create)
        session = await settled(manager, create["session_id"])
        first = attach_request(manager, create)
        await manager.control("attach", first, project())
        remote_identity = session.public()["tmux_ref"]
        await manager.control("detach", first, project())
        clock[0] = manager.config.attachment_grace_seconds + 1
        session.lease_until = clock[0] + manager.config.lease_seconds
        retiring = asyncio.create_task(manager.sweep())
        pending.append(retiring)
        await asyncio.wait_for(closing.wait(), 2)
        second = {
            **first,
            "attachment_id": uuid.uuid4().hex,
            "render_epoch": "2",
            "writer_epoch": "2",
        }
        attaching = asyncio.create_task(manager.control("attach", second, project()))
        pending.append(attaching)
        await asyncio.sleep(0)
        assert not attaching.done(), "attach raced past the in-flight channel retirement"
        release.set()
        await asyncio.wait_for(retiring, 2)
        result = await asyncio.wait_for(attaching, 2)
        assert result["state"] == "ready"
        assert result["resume_strategy"] == "redraw"
        assert (
            result["tmux_ref"] == remote_identity and result["session_id"] == create["session_id"]
        )
        assert transport.channel_closed is False and session.ended is None
        assert remote["creates"] == 1
        assert not any("kill-session" in command for command in remote["commands"])
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
        await manager.close_all()
