"""Real OS exclusion between an Agent's Web SSH owner and the local installer.

Peers use synthetic identities and temporary state only. No service is restarted,
no SSH endpoint is contacted, and a lock-file's mere existence is never busy.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import agent.ssh_terminal_manager as manager_module
from agent.ssh_terminal_manager import TerminalSessionManager
from agent.ssh_terminal_transport import SSHTransportError
from scripts import install_agent as installer
from shared.ssh_terminal_protocol import FrameKind, TerminalFrame, TerminalProtocolError
from tests.test_ssh_terminal_agent import (
    FakeTmuxTransport,
    FakeTransport,
    admitted_create,
    attach_request,
    create_request,
    project,
    settled,
)


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_agent.py"
pytestmark = pytest.mark.integration


_LOCK_PROCESS = """
import runpy, sys
from pathlib import Path
api = runpy.run_path(sys.argv[1])
base, state = Path(sys.argv[2]), Path(sys.argv[3])
marker = base / '.install.lock'
holding = sys.argv[4] == 'hold'
with_marker = sys.argv[5] == 'marker'
if holding and with_marker:
    marker.mkdir()
try:
    try:
        lock = api['WebSSHMaintenanceLock'](base, {'state_dir': str(state)})
    except ValueError as error:
        if not str(error).startswith('WEB_SSH_BUSY:'):
            raise
        print('BUSY', flush=True)
        sys.exit(73)
    try:
        if holding:
            Path(sys.argv[6]).write_text('ready')
            sys.stdin.read(1)
        print('IDLE', flush=True)
    finally:
        lock.close()
finally:
    if holding and with_marker:
        marker.rmdir()
"""


def probe(base, state):
    # -S also removes site packages: the downloaded installer really works alone.
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _LOCK_PROCESS, str(INSTALLER),
         str(base), str(state), "probe", "no-marker"],
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode in {0, 73}, result.stderr
    assert result.stdout.strip() == ("IDLE" if result.returncode == 0 else "BUSY")
    return result.returncode == 0


@contextmanager
def installer_process(base, state, *, marker=False, allow_kill=False):
    base.mkdir(parents=True, exist_ok=True)
    ready = base / "test-lock-ready"
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", _LOCK_PROCESS, str(INSTALLER), str(base),
         str(state), "hold", "marker" if marker else "no-marker", str(ready)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while not ready.exists():
            if child.poll() is not None:
                stdout, stderr = child.communicate(timeout=2)
                pytest.fail(f"isolated lock holder failed: {stdout} {stderr}")
            assert time.monotonic() < deadline, "isolated lock holder did not become ready"
            time.sleep(0.01)
        yield child
    finally:
        try:
            stdout, stderr = child.communicate("x" if child.poll() is None else None, timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=3)
            raise
        if not allow_kill:
            assert child.returncode == 0, stdout + stderr


def make_manager(tmp_path, *, connector=None, clock=time.monotonic):
    base, state = tmp_path / "installation", tmp_path / "separate-state"
    base.mkdir(exist_ok=True)
    owner = SimpleNamespace(state_dir=state,
                            lifecycle=SimpleNamespace(base=base, handoff_pending=False))
    manager = TerminalSessionManager(owner, emit=lambda _: None, connector=connector or FakeTransport,
                                     authorize=lambda _: None, clock=clock)
    return manager, base, state


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", ["reservation", "direct", "detached_tmux", "unknown_close"])
async def test_live_or_unconfirmed_webssh_blocks_independent_installer(tmp_path, activity):
    now, remote = [0.0], {}
    manager, base, state = make_manager(
        tmp_path, connector=(lambda: FakeTmuxTransport(remote)) if "tmux" in activity
        or activity == "unknown_close" else FakeTransport, clock=lambda: now[0],
    )
    request = create_request(manager, mode="direct" if activity in {"direct", "reservation"} else "tmux")
    try:
        assert probe(base, state)
        if activity == "reservation":
            await manager.control("reserve_create", request, project())
        else:
            await admitted_create(manager, request)
            session = await settled(manager, request["session_id"])
            if activity == "detached_tmux":
                now[0] += manager.config.lease_seconds + 1
                await manager.sweep()
                assert session.transport is None and remote["exists"]
            elif activity == "unknown_close":
                remote["uncertain_close"] = True
                await manager.control("close", request, project())
                assert session.state == "close_unknown"
        assert manager.maintenance_busy()
        assert not probe(base, state)
        remote["uncertain_close"] = False
        result = await manager.control("close", request, project())
        assert result["remote_end_confirmed"] is True
        assert not manager.maintenance_busy()
        assert probe(base, state)
        assert (state / ".ssh-terminal-activity.lock").is_file()
        if activity != "reservation":
            assert request["session_id"] in manager.sessions  # Closed history is retained, but idle.
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_expired_create_permit_releases_activity_lock_without_a_create(tmp_path):
    now = [0.0]
    manager, base, state = make_manager(tmp_path, clock=lambda: now[0])
    try:
        await manager.control("reserve_create", create_request(manager), project())
        assert manager._sweep_task is not None and not manager._sweep_task.done()
        assert not probe(base, state)
        now[0] = 31.0
        await manager.sweep()
        assert not manager.maintenance_busy()
        assert probe(base, state)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_tmux", [False, True])
async def test_definitively_failed_setup_releases_lock_after_tasks_finish(tmp_path, missing_tmux):
    class FailedConnect(FakeTransport):
        async def connect(self, *args, **kwargs):
            raise SSHTransportError("SSH_AUTH_FAILED", "synthetic authentication failure")

    manager, base, state = make_manager(
        tmp_path, connector=(lambda: FakeTmuxTransport({})) if missing_tmux else FailedConnect,
    )
    request = create_request(manager, mode="tmux" if missing_tmux else "direct", recover=missing_tmux)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        assert session.state == "failed"
        assert session.reason == ("REMOTE_SESSION_MISSING" if missing_tmux else "SSH_AUTH_FAILED")
        assert not manager.maintenance_busy() and probe(base, state)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [False, True])
async def test_installer_first_fences_new_activity_but_idle_manager_starts(tmp_path, marker):
    manager, base, state = make_manager(tmp_path)
    try:
        with installer_process(base, state, marker=marker):
            # Constructors/capabilities are used during the new Agent handshake.
            assert manager.capabilities()["protocol"] == 1
            assert not manager.maintenance_busy() and manager._activity_lock is None
            with pytest.raises(TerminalProtocolError) as error:
                await manager.control("reserve_create", create_request(manager), project())
            assert error.value.code == "AGENT_MAINTENANCE"
            assert not manager._admissions and not manager.sessions
        request = create_request(manager)
        await manager.control("reserve_create", request, project())
        assert not probe(base, state)
        await manager.control("close", request, project())
        assert probe(base, state)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_installer_created_between_precheck_and_lock_acquire_is_rechecked(tmp_path, monkeypatch):
    manager, base, state = make_manager(tmp_path)
    real_lock = manager_module.InstanceLock

    def interleaving(path):
        lock = real_lock(path)
        (base / ".install.lock").mkdir()
        return lock

    monkeypatch.setattr(manager_module, "InstanceLock", interleaving)
    try:
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control("reserve_create", create_request(manager), project())
        assert error.value.code == "AGENT_MAINTENANCE"
        assert not manager._admissions and manager._activity_lock is None
        assert probe(base, state)  # The rejected acquisition closed its descriptor.
    finally:
        (base / ".install.lock").rmdir()
        await manager.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["--upgrade", "--start-service"])
async def test_real_standalone_cli_rejects_active_webssh_before_service_actions(tmp_path, action):
    manager, base, state = make_manager(tmp_path)
    config = {"device_id": "a" * 32, "hub_url": "http://127.0.0.1:1",
              "state_dir": str(state), "secret": "synthetic-must-not-appear"}
    raw = json.dumps(config)
    (base / "config.json").write_text(raw)
    request = create_request(manager)
    try:
        await manager.control("reserve_create", request, project())
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(INSTALLER), action, "--install-dir", str(base)],
            cwd=tmp_path, capture_output=True, text=True, timeout=8,
        )
        assert result.returncode == 1
        assert "WEB_SSH_BUSY:" in result.stderr and config["secret"] not in result.stdout + result.stderr
        assert (base / "config.json").read_text() == raw
        assert not (base / "runtime").exists() and not (base / ".install.lock").exists()
        await manager.control("close", request, project())
        assert probe(base, state)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_interrupted_installer_releases_os_lock_but_keeps_directory_fence(tmp_path):
    manager, base, state = make_manager(tmp_path)
    try:
        with installer_process(base, state, marker=True, allow_kill=True) as child:
            assert not probe(base, state)
            child.kill()
            child.wait(timeout=5)
            assert probe(base, state)  # No stale OS lock after the owner's death.
            assert (base / ".install.lock").is_dir()
            with pytest.raises(TerminalProtocolError) as error:
                await manager.control("reserve_create", create_request(manager), project())
            assert error.value.code == "AGENT_MAINTENANCE"
        # A local owner would first inspect the interrupted installation.
        (base / ".install.lock").rmdir()
        request = create_request(manager)
        await manager.control("reserve_create", request, project())
        await manager.control("close", request, project())
        assert probe(base, state)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_install_directory_fences_queued_input_before_ssh_write_and_resize(tmp_path):
    transport = FakeTransport()
    manager, base, state = make_manager(tmp_path, connector=lambda: transport)
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attachment = attach_request(manager, request)
        await manager.control("attach", attachment, project())
        transport.block_write = asyncio.Event()
        frame = lambda sequence: TerminalFrame(FrameKind.INPUT, session.id, manager.generation,
                                               session.stream_epoch, session.writer_epoch, sequence, b"x")
        first = asyncio.create_task(manager.input(frame(1)))
        async with asyncio.timeout(2):
            while not transport.writes:
                await asyncio.sleep(0)
        pending = asyncio.create_task(manager.input(frame(2)))
        async with asyncio.timeout(2):
            while session.queued_input_bytes != 2:
                await asyncio.sleep(0)
        (base / ".install.lock").mkdir()
        transport.block_write.set()
        assert (await first)["status"] == "forwarded"
        with pytest.raises(TerminalProtocolError) as error:
            await asyncio.wait_for(pending, 2)
        assert error.value.code == "AGENT_MAINTENANCE"
        assert transport.writes == [b"x"] and session.tracker.next_input_seq == 2
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control("resize", {**attachment, "resize_seq": "1", "cols": 88, "rows": 22}, project())
        assert error.value.code == "AGENT_MAINTENANCE"
        # Explicit close remains available to resolve the activity, even while fenced.
        await manager.control("close", request, project())
        assert probe(base, state)
    finally:
        (base / ".install.lock").rmdir()
        await manager.close_all()


_NEW_AGENT_HEALTH = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent.runner import Agent
from scripts.install_agent import require_idle
from shared.ssh_terminal_protocol import TerminalProtocolError
base = Path(sys.argv[2])
current = json.loads((base / 'config.json').read_text())
require_idle(base, current)  # A child helper must not reacquire its parent's lock.
async def check():
    agent = Agent(base / 'config.json')
    try:
        assert agent.ssh_terminal.capabilities()['protocol'] == 1
        agent.check_maintenance_idle('synthetic-service-verification')
        try:
            agent.ssh_terminal.manager._acquire_activity_lock()
        except TerminalProtocolError as error:
            assert error.code == 'AGENT_MAINTENANCE'
        else:
            raise AssertionError('new terminal escaped the parent installer lock')
        print('IDLE_AGENT_READY', flush=True)
    finally:
        await agent.ssh_terminal.close()
        await agent.integrations.close()
        await agent.computer.close()
        agent.journal.db.close()
        agent.instance_lock.close()
asyncio.run(check())
"""


@pytest.mark.parametrize("failure", [False, True])
def test_cli_holds_activity_lock_through_helper_and_new_agent_health(tmp_path, monkeypatch, failure):
    base, state = tmp_path / "installation", tmp_path / "separate-state"
    base.mkdir()
    current = {"device_id": "a" * 32, "hub_url": "http://127.0.0.1:1", "name": "Synthetic Agent",
               "secret": "synthetic-only-" * 5, "state_dir": str(state),
               "allowed_roots": [{"path": str(tmp_path), "writable": True, "allow_tasks": True}],
               "tasks": {}, "shell": {"enabled": True, "projects": ["*"]}}
    (base / "config.json").write_text(json.dumps(current))

    def staged_upgrade(actual_base, args, actual_current):
        assert actual_base == base and actual_current == current
        assert (base / ".install.lock").is_dir()
        assert not probe(base, state)
        # The real new Agent constructor, journal and lifecycle admission work
        # in a separate process while the CLI owns the activity lock.
        child = subprocess.run(
            [sys.executable, "-I", "-c", _NEW_AGENT_HEALTH, str(ROOT), str(base)],
            cwd=tmp_path, capture_output=True, text=True, timeout=20,
        )
        assert child.returncode == 0, child.stdout + child.stderr
        assert "IDLE_AGENT_READY" in child.stdout
        assert not probe(base, state)
        if failure:
            raise RuntimeError("synthetic helper rollback")

    monkeypatch.setattr(installer, "local_upgrade", staged_upgrade)
    monkeypatch.setattr(installer.sys, "argv", ["install_agent.py", "--upgrade", "--install-dir", str(base)])
    if failure:
        with pytest.raises(RuntimeError, match="synthetic helper rollback"):
            installer.main()
    else:
        installer.main()
    assert not (base / ".install.lock").exists()
    assert probe(base, state)
    assert json.loads((base / "config.json").read_text()) == current


def test_standalone_installer_remains_python39_syntax():
    ast.parse(INSTALLER.read_text(), filename=str(INSTALLER), feature_version=(3, 9))
