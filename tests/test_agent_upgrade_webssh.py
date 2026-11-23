"""Safety checks for Agent maintenance around independent terminal work."""

from types import SimpleNamespace

import pytest

from agent.runner import Agent
from agent.ssh_terminal_manager import TerminalSessionManager
from scripts.install_agent import WebSSHMaintenanceLock
from shared.instance_lock import InstanceLock
from shared.util import DevError
from tests.test_ssh_terminal_agent import (
    FakeTmuxTransport,
    admitted_create,
    create_request,
    project,
    settled,
)


def test_busy_webssh_reports_a_hold_without_requesting_remote_termination():
    terminal = SimpleNamespace(maintenance_busy=lambda: True)
    owner = SimpleNamespace(
        computer=SimpleNamespace(session=None), ssh_terminal=SimpleNamespace(manager=terminal)
    )
    with pytest.raises(DevError) as error:
        Agent.check_maintenance_idle(owner, "fixture-update")
    assert error.value.code == "AGENT_BUSY"
    assert error.value.details["reason_code"] == "WEB_SSH_ACTIVITY"
    assert "Agent 维护未开始" in error.value.message
    assert "不要为升级结束 tmux" in error.value.message


def test_cli_maintains_webssh_lock_without_requesting_remote_termination(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    path = state / ".ssh-terminal-activity.lock"
    live = InstanceLock(path)
    try:
        with pytest.raises(ValueError, match="WEB_SSH_BUSY") as error:
            WebSSHMaintenanceLock(tmp_path, {"state_dir": str(state)})
        assert "do not end it" in str(error.value)
        assert path.is_file()
    finally:
        live.close()
    # Same lock file may be reused when the activity owner really releases it.
    with WebSSHMaintenanceLock(tmp_path, {"state_dir": str(state)}):
        assert path.is_file()


@pytest.mark.asyncio
async def test_two_agent_generations_preserve_the_exact_remote_tmux_without_killing_it():
    remote = {}

    def make_manager():
        return TerminalSessionManager(
            emit=lambda _: None,
            connector=lambda: FakeTmuxTransport(remote),
            authorize=lambda _: None,
        )

    old = make_manager()
    request = create_request(old, mode="tmux")
    sid = request["session_id"]
    try:
        await admitted_create(old, request)
        first = await settled(old, sid)
        original_ref = first.tmux.reference
        original_transport = first.transport
        assert first.state == "ready" and original_ref.session == "cp_" + sid
    finally:
        await old.close_all()
    assert original_transport.closed
    assert remote["exists"] and remote["creates"] == 1
    assert not any("kill-session" in cmd for cmd in remote["commands"])

    replacement = make_manager()
    try:
        recovery = create_request(replacement, session_id=sid, mode="tmux", recover=True)
        assert recovery["runtime_generation"] != request["runtime_generation"]
        await admitted_create(replacement, recovery)
        restored = await settled(replacement, sid)
        assert restored.state == "ready" and restored.tmux.reference == original_ref
        assert remote["exists"] and remote["creates"] == 1
    finally:
        await replacement.close_all()
    assert remote["exists"] and not any("kill-session" in cmd for cmd in remote["commands"])
