"""Agent SSH behavior against isolated peers and deterministic transports."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

import pytest

from agent.ssh_terminal_transport import AsyncSSHTransport, SSHTransportError
from agent.ssh_terminal_manager import TerminalSessionManager
from agent.ssh_terminal_tmux import TmuxController, TmuxReference
from agent.ssh_terminal_trust import HostKeyPolicy, HostTrustError, check_native_trust_configuration
from shared.ssh_terminal_config import DEFAULT_CONFIG
from shared.ssh_terminal_protocol import FrameKind, TerminalFrame, TerminalProtocolError


def project():
    return {
        "id": "project-a",
        "root": ".",
        "mode": "write",
        "allow_tasks": True,
        "_terminal_owner": "user:owner-a",
        "_terminal_space": "space-a",
    }


def create_request(manager, **changes):
    return {
        "session_id": uuid.uuid4().hex,
        "runtime_generation": manager.generation,
        "owner_user_id": "owner-a",
        "space_id": "space-a",
        "vps_id": "vps-a",
        "connection_revision": 1,
        "binding_id": "binding-a",
        "authority_revision": "authority-a",
        "host": "server.example.invalid",
        "port": 22,
        "username": "fixture",
        "password": "fixture-secret-only",
        "host_key_policy": "strict",
        "mode": "direct",
        "cols": 120,
        "rows": 30,
        **changes,
    }


async def admitted_create(manager, request, scope=None):
    scope = scope or project()
    grant = await manager.control("reserve_create", request, scope)
    request["admission_token"] = grant["admission_token"]
    return await manager.control("create", request, scope)


@pytest.mark.asyncio
async def test_create_admission_is_scoped_single_use_expiring_and_bounded():
    current, connectors = [0.0], []

    def connector():
        transport = FakeTransport()
        connectors.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=lambda _: None, connector=connector, authorize=lambda _: None, clock=lambda: current[0]
    )
    request = create_request(manager)
    try:
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control("create", request, project())
        assert error.value.code == "CREATE_ADMISSION_REQUIRED" and not connectors
        reservation = await manager.control("reserve_create", request, project())
        assert (await manager.control("reserve_create", request, project()))[
            "admission_token"
        ] == reservation["admission_token"]
        request["admission_token"] = reservation["admission_token"]
        with pytest.raises(TerminalProtocolError):
            await manager.control("create", {**request, "connection_revision": 2}, project())
        with pytest.raises(TerminalProtocolError):
            await manager.control(
                "create",
                {**request, "owner_user_id": "other"},
                {**project(), "_terminal_owner": "user:other"},
            )
        assert not connectors
        current[0] = 31
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control("create", request, project())
        assert error.value.code == "CREATE_ADMISSION_REQUIRED"
        await admitted_create(manager, request)
        await settled(manager, request["session_id"])
        assert request["session_id"] not in manager._admissions
        assert (await manager.control("reserve_create", request, project()))[
            "admission_token"
        ] is None
        await manager.control("create", request, project())
        assert len(connectors) == 1
        for _ in range(manager._admission_limit):
            await manager.control("reserve_create", create_request(manager), project())
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control("reserve_create", create_request(manager), project())
        assert error.value.code == "ADMISSION_LIMIT"
        current[0] += 31
        await manager.control("reserve_create", create_request(manager), project())
        assert len(manager._admissions) == 1
    finally:
        await manager.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["close", "revoke"])
async def test_pending_create_admission_can_be_closed_or_revoked_before_delayed_create(action):
    connections = []
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: connections.append(True), authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        grant = await manager.control("reserve_create", request, project())
        request["admission_token"] = grant["admission_token"]
        stopped = await manager.control(action, request, project())
        assert stopped["remote_end_confirmed"] and not stopped["input_enabled"]
        late = await manager.control("create", request, project())
        assert late["state"] in {"exited", "failed"}
        assert (await manager.control("reserve_create", request, project()))[
            "admission_token"
        ] is None
        assert request["session_id"] not in manager.sessions and not connections
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_many_closed_sessions_stay_bounded_and_old_creates_cannot_reopen():
    import json

    remote, connectors = {"uncertain_create": True}, []
    config = replace(DEFAULT_CONFIG, max_sessions_per_agent=2, max_sessions_per_user=2)

    def connector():
        transport = FakeTmuxTransport(remote)
        connectors.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=lambda _: None, connector=connector, config=config, authorize=lambda _: None
    )
    uncertain = create_request(manager, mode="tmux")
    requests = {}
    try:
        await admitted_create(manager, uncertain)
        unresolved = await settled(manager, uncertain["session_id"])
        assert unresolved.mode == "tmux" and unresolved.state == "failed"
        assert unresolved.stopped.is_set()
        for _ in range(manager._metadata_limit + manager._tombstone_limit + 12):
            request = create_request(manager)
            await admitted_create(manager, request)
            await settled(manager, request["session_id"])
            await manager.control("close", request, project())
            requests[request["session_id"]] = dict(request)
            assert len(manager.sessions) <= manager._metadata_limit
            assert len(manager._tombstones) <= manager._tombstone_limit
        assert uncertain["session_id"] in manager.sessions and remote["creates"] == 1
        assert (
            sum(item.ring.capacity_bytes for item in manager.sessions.values())
            <= manager._metadata_limit
        )
        first = next(iter(requests.values()))
        assert (
            first["session_id"] not in manager.sessions
            and first["session_id"] not in manager._tombstones
        )
        before = len(connectors)
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control("create", first, project())
        assert error.value.code == "CREATE_ADMISSION_REQUIRED" and len(connectors) == before
        tombstone_id = next(iter(manager._tombstones))
        retired = await manager.control("create", requests[tombstone_id], project())
        assert retired["state"] == "exited" and len(connectors) == before
        assert (await manager.control("status", requests[tombstone_id], project()))[
            "state"
        ] == "exited"

        page = await manager.control("list", {}, project())
        assert len(page["sessions"]) <= 32 and page["truncated"]
        listed = []
        while True:
            assert (
                len(json.dumps(page, ensure_ascii=True, separators=(",", ":")).encode()) < 48 * 1024
            )
            assert len(page["sessions"]) <= 64 and page["retention"] == "active_and_recent"
            listed.extend(item["session_id"] for item in page["sessions"])
            if not page["truncated"]:
                assert page["next_cursor"] is None
                break
            page = await manager.control(
                "list", {"cursor": page["next_cursor"], "limit": 64}, project()
            )
        assert len(listed) == len(set(listed)) == len(manager.sessions)
        assert set(listed) == set(manager.sessions)
        assert not (await manager.control("list", {}, {**project(), "root": "/different-project"}))[
            "sessions"
        ]
    finally:
        await manager.close_all()


def test_metadata_limits_remain_small_at_maximum_configured_session_capacity():
    manager = TerminalSessionManager(
        emit=lambda _: None,
        authorize=lambda _: None,
        config=replace(DEFAULT_CONFIG, max_sessions_per_agent=256),
    )
    assert manager._metadata_limit == manager._tombstone_limit == 1024
    assert manager._admission_limit == 256


@pytest.mark.parametrize("extractor", ["installer", "current_updater", "legacy_updater"])
def test_downloaded_agent_archive_contains_importable_terminal_and_keeps_old_updater_compatibility(
    tmp_path, extractor
):
    import importlib.util
    import io
    import subprocess
    import sys
    import zipfile

    from agent.lifecycle import LifecycleManager
    from hub.agent_install import AgentPackage
    from scripts import install_agent

    root = Path(__file__).resolve().parents[1]
    expected = {
        "agent/ssh_terminal.py",
        "agent/ssh_terminal_manager.py",
        "agent/ssh_terminal_transport.py",
        "agent/ssh_terminal_trust.py",
        "agent/ssh_terminal_tmux.py",
        "agent/ssh_terminal_link.py",
        "shared/ssh_terminal_protocol.py",
        "shared/ssh_terminal_config.py",
        "shared/ssh_terminal_flow.py",
        "shared/ssh_terminal_channel.py",
    }
    package = AgentPackage(root).build()
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        assert expected <= set(archive.namelist())
        assert not any(name.startswith("agent/ssh_terminal/") for name in archive.namelist())
        assert b"asyncssh==2.24.1" in archive.read("requirements-agent.txt")
    archive = tmp_path / "agent.zip"
    archive.write_bytes(package.content)
    destination = tmp_path / "installed-runtime"
    if extractor == "installer":
        install_agent.unpack(archive, package.sha256, destination)
    elif extractor == "current_updater":
        LifecycleManager._extract(archive, package.sha256, destination)
    else:
        spec = importlib.util.spec_from_file_location(
            "legacy_terminal_package_check", root / "tests/fixtures/legacy-agent-lifecycle-v180.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.LifecycleManager._extract(archive, package.sha256, destination)
    # -I ignores the checkout and PYTHONPATH. Only extracted runtime modules,
    # stdlib and the already verified dependency environment can satisfy imports.
    verification = """
import importlib, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
for name in ('agent.runner', 'agent.ssh_terminal', 'agent.ssh_terminal_manager',
             'agent.ssh_terminal_transport', 'agent.ssh_terminal_trust',
             'agent.ssh_terminal_tmux', 'agent.ssh_terminal_link',
             'shared.ssh_terminal_protocol', 'shared.ssh_terminal_channel'):
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
from agent.ssh_terminal import TerminalSessionManager
assert TerminalSessionManager(emit=lambda _: None).capabilities()['protocol'] == 1
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", verification, str(destination)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


async def settled(manager, sid):
    await asyncio.wait_for(manager.sessions[sid].start_task, 2)
    return manager.sessions[sid]


def attach_request(manager, request, **changes):
    return {
        "session_id": request["session_id"],
        "runtime_generation": manager.generation,
        "vps_id": request["vps_id"],
        "binding_id": request["binding_id"],
        "connection_revision": request["connection_revision"],
        "attachment_id": uuid.uuid4().hex,
        "renderer_id": uuid.uuid4().hex,
        "render_epoch": "1",
        "writer_epoch": "1",
        "stream_epoch": "1",
        "rendered_offset": "0",
        **changes,
    }


class FakeTransport:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.channel_closed = False
        self.paused = False
        self.block_write = None

    async def connect(self, target, password, *, challenge=None):
        self.target = {k: v for k, v in target.items()}
        return self

    async def open_pty(self, cols, rows, on_data, on_exit, *, command=None):
        self.on_data, self.on_exit = on_data, on_exit
        self.size = (cols, rows)
        self.command = command
        self.channel_closed = False

    def write(self, data):
        self.writes.append(bytes(data))

    async def wait_writable(self):
        if self.block_write is not None:
            await self.block_write.wait()

    def resize(self, cols, rows):
        self.size = (cols, rows)

    def pause_output(self):
        self.paused = True

    def resume_output(self):
        self.paused = False

    async def close_channel(self):
        self.channel_closed = True

    async def close(self):
        self.closed = True


class FakeTmuxTransport(FakeTransport):
    def __init__(self, remote):
        super().__init__()
        self.remote = remote

    async def run_control(self, command, **kwargs):
        self.remote.setdefault("commands", []).append(command)
        if "command -v tmux" in command:
            return 0, b"/usr/bin/tmux\ntmux 3.5a\n"
        if "has-session" in command:
            return (0, b"") if self.remote.get("exists") else (1, b"no server running\n")
        if "new-session" in command:
            self.remote["creates"] = self.remote.get("creates", 0) + 1
            self.remote["exists"] = True
            if self.remote.get("uncertain_create"):
                raise SSHTransportError("SSH_CONTROL_UNCERTAIN", "fixture unknown create result")
            return 0, b""
        if "kill-session" in command:
            self.remote["exists"] = False
            if self.remote.get("uncertain_close"):
                raise SSHTransportError("SSH_CONTROL_UNCERTAIN", "fixture unknown close result")
            return 0, b""
        raise AssertionError("Unexpected control command")


def maintenance_owner(manager, lifecycle):
    from agent.runner import Agent

    owner = SimpleNamespace(
        lifecycle=lifecycle,
        computer=SimpleNamespace(session=None),
        jobs={},
        processes={},
        transfer_tasks=set(),
        kill_tasks=set(),
        ssh_terminal=SimpleNamespace(manager=manager),
    )
    manager.agent = owner
    lifecycle.activity_check = lambda operation: Agent.check_maintenance_idle(owner, operation)
    return owner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activity",
    [
        "reservation",
        "creating",
        "direct",
        "tail",
        "detached_tmux",
        "uncertain_tmux_create",
        "uncertain_tmux_close",
    ],
)
async def test_managed_maintenance_requires_confirmed_webssh_retirement(
    tmp_path, monkeypatch, activity
):
    from shared.util import DevError
    from tests.test_agent_lifecycle import managed_manager

    lifecycle, base = managed_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle, "actions", lambda: {"agent_restart"})
    current, remote, prepared = [0.0], {}, []
    entered, release = asyncio.Event(), asyncio.Event()

    class Starting(FakeTransport):
        async def connect(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().connect(*args, **kwargs)

    def connector():
        if "tmux" in activity:
            return FakeTmuxTransport(remote)
        return Starting() if activity == "creating" else FakeTransport()

    def prepare(operation, action):
        prepared.append(action)
        lifecycle._write_plan(operation, {"action": action, "base": str(base)})
        return {"stage": "prepared"}

    monkeypatch.setattr(lifecycle, "_prepare_control", prepare)
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=connector, authorize=lambda _: None, clock=lambda: current[0]
    )
    maintenance_owner(manager, lifecycle)
    request = create_request(manager, mode="tmux" if "tmux" in activity else "direct")
    try:
        if activity == "reservation":
            await manager.control("reserve_create", request, project())
        else:
            remote["uncertain_create"] = activity == "uncertain_tmux_create"
            await admitted_create(manager, request)
            if activity == "creating":
                await asyncio.wait_for(entered.wait(), 1)
                assert not manager.sessions[request["session_id"]].start_task.done()
            else:
                session = await settled(manager, request["session_id"])
                if activity in {"direct", "tail", "detached_tmux"}:
                    attachment = attach_request(manager, request)
                    await manager.control("attach", attachment, project())
                if activity == "tail":
                    session.transport.on_data(b"unconsumed final output")
                    session.transport.on_exit(
                        {"output_complete": True, "exit_status": 0, "reason_code": "SSH_EXITED"}
                    )
                    assert session.state == "exited" and session.ring.size > 0
                if activity == "detached_tmux":
                    await manager.control("detach", attachment, project())
                    current[0] += manager.config.lease_seconds + 1
                    await manager.sweep()
                    assert session.transport is None and remote["exists"]
                    assert session.public()["recoverability"] == "reauthenticate"
                if activity == "uncertain_tmux_create":
                    assert (
                        session.state == "failed" and session.stopped.is_set() and remote["exists"]
                    )
                if activity == "uncertain_tmux_close":
                    remote["uncertain_close"] = True
                    await manager.control("close", request, project())
                    assert session.state == "close_unknown"
        with pytest.raises(DevError) as error:
            await lifecycle.prepare("webssh-maintenance", "agent_restart", {})
        assert error.value.code == "AGENT_BUSY" and "Web SSH" in error.value.message
        assert not prepared and not lifecycle.handoff_pending
        # Closing the exact ID, including uncertain detached tmux, is explicit.
        # A terminal tab detach or a failed local status never fakes remote end.
        remote["uncertain_close"] = False
        result = await manager.control("close", request, project())
        assert result["remote_end_confirmed"] is True
        assert not manager.maintenance_busy()
        assert (await lifecycle.prepare("webssh-maintenance", "agent_restart", {}))[
            "stage"
        ] == "prepared"
        assert prepared == ["agent_restart"]
        if activity != "reservation":
            assert request["session_id"] in manager.sessions  # Retained history doesn't stay busy.
    finally:
        release.set()
        await manager.close_all()


@pytest.mark.asyncio
async def test_maintenance_ignores_expired_admission_and_confirmed_missing_tmux():
    current = [0.0]
    manager = TerminalSessionManager(
        emit=lambda _: None,
        connector=lambda: FakeTmuxTransport({}),
        authorize=lambda _: None,
        clock=lambda: current[0],
    )
    try:
        await manager.control("reserve_create", create_request(manager), project())
        assert manager.maintenance_busy()
        current[0] = 31
        assert not manager.maintenance_busy()
        request = create_request(manager, mode="tmux", recover=True)
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        assert session.reason == "REMOTE_SESSION_MISSING" and session.state == "failed"
        assert not manager.maintenance_busy()
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_maintenance_ack_rechecks_webssh_and_keeps_plan_until_explicit_close(
    tmp_path, monkeypatch
):
    from shared.util import DevError
    from tests.test_agent_lifecycle import managed_manager

    lifecycle, base = managed_manager(tmp_path, monkeypatch)
    operation, launched = "webssh-maintenance", []
    lifecycle._write_plan(
        operation,
        {
            "action": "agent_restart",
            "base": str(base),
            "helper": str(base / "helper.py"),
            "python": str(base / "python"),
        },
    )
    monkeypatch.setattr(lifecycle, "_launch_helper", lambda *_: launched.append(True))
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=FakeTransport, authorize=lambda _: None
    )
    maintenance_owner(manager, lifecycle)
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        await settled(manager, request["session_id"])
        with pytest.raises(DevError) as error:
            await lifecycle.acknowledge(operation)
        assert error.value.code == "AGENT_BUSY" and not launched
        assert lifecycle.has_pending(operation) and lifecycle.handoff_pending
        assert (await manager.control("close", request, project()))["remote_end_confirmed"]
        assert await lifecycle.acknowledge(operation)
        assert launched == [True] and not lifecycle.has_pending(operation)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect", "recover_close"])
async def test_maintenance_rechecks_inflight_connection_and_recovery(stage):
    lifecycle, remote = SimpleNamespace(handoff_pending=False), {}
    entered, release = asyncio.Event(), asyncio.Event()
    transports = []

    class Delayed(FakeTmuxTransport):
        async def connect(self, *args, **kwargs):
            if stage == "connect":
                entered.set()
                await release.wait()
            return await super().connect(*args, **kwargs)

        async def close(self):
            self.closed = True
            if stage == "recover_close":
                entered.set()
                await release.wait()

    def connector():
        transport = Delayed(remote) if not transports else FakeTmuxTransport(remote)
        transports.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=lambda _: None, connector=connector, authorize=lambda _: None
    )
    maintenance_owner(manager, lifecycle)
    request = create_request(manager, mode="tmux")
    recovery = None
    try:
        await admitted_create(manager, request)
        if stage == "recover_close":
            session = await settled(manager, request["session_id"])
            session.transport.closed = True
            recovery = asyncio.create_task(
                manager.control("recover", {**request, "authority_revision": "renewed"}, project())
            )
        await asyncio.wait_for(entered.wait(), 1)
        lifecycle.handoff_pending = True
        release.set()
        if recovery is not None:
            with pytest.raises(TerminalProtocolError) as error:
                await recovery
            assert error.value.code == "AGENT_MAINTENANCE" and remote["creates"] == 1
            assert manager.maintenance_busy()  # Original remote tmux still exists.
        else:
            session = await settled(manager, request["session_id"])
            assert session.state == "failed" and session.reason == "AGENT_MAINTENANCE"
            assert not remote.get("creates") and not hasattr(transports[0], "on_data")
        assert len(transports) == 1
    finally:
        release.set()
        await manager.close_all()
        if recovery is not None:
            await asyncio.gather(recovery, return_exceptions=True)


@pytest.mark.asyncio
async def test_maintenance_fences_new_and_queued_input_resize_and_link_receipts():
    from agent.ssh_terminal_link import AgentTerminalLink
    from shared.ssh_terminal_channel import TerminalSendQueue

    lifecycle = SimpleNamespace(handoff_pending=False)
    transport = FakeTransport()
    transport.block_write = asyncio.Event()
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: transport, authorize=lambda _: None
    )
    owner = maintenance_owner(manager, lifecycle)
    request = create_request(manager)
    tasks = []
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attachment = attach_request(manager, request)
        await manager.control("attach", attachment, project())
        frame = lambda seq: TerminalFrame(
            FrameKind.INPUT, session.id, manager.generation, 1, 1, seq, b"x"
        )
        first = asyncio.create_task(manager.input(frame(1)))
        tasks.append(first)
        async with asyncio.timeout(1):
            while not transport.writes:
                await asyncio.sleep(0)
        queued = asyncio.create_task(manager.input(frame(2)))
        tasks.append(queued)
        async with asyncio.timeout(1):
            while session.queued_input_bytes != 2:
                await asyncio.sleep(0)
        lifecycle.handoff_pending = True
        with pytest.raises(TerminalProtocolError) as error:
            await manager.input(frame(3))
        assert error.value.code == "AGENT_MAINTENANCE"
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control(
                "resize", {**attachment, "resize_seq": "1", "cols": 80, "rows": 24}, project()
            )
        assert error.value.code == "AGENT_MAINTENANCE" and transport.size == (120, 30)
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control("reserve_create", create_request(manager), project())
        assert error.value.code == "AGENT_MAINTENANCE"
        transport.block_write.set()
        assert (await first)[
            "status"
        ] == "forwarded"  # It was written before handoff, never repeated.
        with pytest.raises(TerminalProtocolError) as error:
            await queued
        assert error.value.code == "AGENT_MAINTENANCE"
        assert transport.writes == [b"x"] and session.tracker.next_input_seq == 2
        assert session.queued_input_bytes == 0
        link, queue = AgentTerminalLink(owner), TerminalSendQueue(manager.config)
        link.manager = manager
        await link._input(frame(3), queue)
        assert (await queue.get())["reason_code"] == "AGENT_MAINTENANCE"
        await link._control(
            {
                "request_id": uuid.uuid4().hex,
                "action": "resize",
                "payload": {**attachment, "resize_seq": "1", "cols": 80, "rows": 24},
                "project": project(),
            },
            queue,
        )
        assert (await queue.get())["error"]["code"] == "AGENT_MAINTENANCE"
        assert (await manager.control("status", request, project()))["session_id"] == session.id
        assert (await manager.control("close", request, project()))["remote_end_confirmed"]
        assert transport.writes == [b"x"] and not manager.maintenance_busy()
        queue.close()
    finally:
        transport.block_write.set()
        await manager.close_all()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_maintenance_rechecks_attach_after_waiting_for_session_lock():
    lifecycle = SimpleNamespace(handoff_pending=False)
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=FakeTransport, authorize=lambda _: None
    )
    maintenance_owner(manager, lifecycle)
    request = create_request(manager)
    pending = None
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        await session.attach_lock.acquire()
        pending = asyncio.create_task(
            manager.control("attach", attach_request(manager, request), project())
        )
        await asyncio.sleep(0)
        lifecycle.handoff_pending = True
        session.attach_lock.release()
        with pytest.raises(TerminalProtocolError) as error:
            await pending
        assert error.value.code == "AGENT_MAINTENANCE" and not session.attached
    finally:
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await manager.close_all()


@pytest.mark.asyncio
async def test_detach_waits_for_in_flight_agent_attach(monkeypatch):
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=FakeTransport, authorize=lambda _: None
    )
    request = create_request(manager)
    entered, resume = asyncio.Event(), asyncio.Event()
    tasks = []
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        original_resume = manager._attachment_resume

        async def delayed_resume(*args):
            entered.set()
            await resume.wait()
            return await original_resume(*args)

        monkeypatch.setattr(manager, "_attachment_resume", delayed_resume)
        pending = attach_request(manager, request)
        tasks.append(asyncio.create_task(manager.control("attach", pending, project())))
        await asyncio.wait_for(entered.wait(), 1)
        tasks.append(asyncio.create_task(manager.control("detach", pending, project())))
        await asyncio.sleep(0)
        resume.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert not session.attached
        assert session.state == "detached"
    finally:
        resume.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close_all()


@pytest.mark.asyncio
async def test_maintenance_allows_only_close_authentication_to_resolve_detached_tmux():
    lifecycle, remote, current = SimpleNamespace(handoff_pending=False), {}, [0.0]
    transports, events = [], []

    class CloseWithOTP(FakeTmuxTransport):
        async def connect(self, target, password, *, challenge=None):
            answers = await challenge("Close authentication", "", [("Code: ", False)])
            assert answers == ["fixture-code"]
            return await super().connect(target, password, challenge=challenge)

    def connector():
        transport = CloseWithOTP(remote) if transports else FakeTmuxTransport(remote)
        transports.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=events.append, connector=connector, authorize=lambda _: None, clock=lambda: current[0]
    )
    maintenance_owner(manager, lifecycle)
    request = create_request(manager, mode="tmux")
    closing = None
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        current[0] += manager.config.lease_seconds + 1
        await manager.sweep()
        lifecycle.handoff_pending = True
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control(
                "auth_answer",
                {**request, "challenge_id": "old", "responses": ["fixture-code"]},
                project(),
            )
        assert error.value.code == "AGENT_MAINTENANCE"
        closing = asyncio.create_task(manager.control("close", request, project()))
        async with asyncio.timeout(1):
            while session.challenge_id is None:
                await asyncio.sleep(0)
        assert (
            await manager.control(
                "auth_answer",
                {**request, "challenge_id": session.challenge_id, "responses": ["fixture-code"]},
                project(),
            )
        )["accepted"]
        result = await closing
        assert result["remote_end_confirmed"] and not remote["exists"]
        assert remote["creates"] == 1 and not manager.maintenance_busy()
    finally:
        if closing is not None and not closing.done():
            closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)
        await manager.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("release", ["ack", "detach", "close", "exit"])
async def test_diagnostics_measure_real_stages_and_one_pause_until_its_lifecycle_end(release):
    from shared.ssh_terminal_channel import TerminalSendQueue
    from shared.util import VERSION

    current, events = [0.0], []

    class Timed(FakeTransport):
        async def connect(self, target, password, *, challenge=None):
            current[0] += 0.125
            return await super().connect(target, password, challenge=challenge)

        async def open_pty(self, *args, **kwargs):
            current[0] += 0.25
            return await super().open_pty(*args, **kwargs)

    transport = Timed()
    config = replace(
        DEFAULT_CONFIG,
        ring_bytes=65536,
        ssh_window_bytes=16384,
        output_high_water_bytes=32768,
        output_low_water_bytes=8192,
    )
    manager = TerminalSessionManager(
        emit=events.append,
        connector=lambda: transport,
        authorize=lambda _: None,
        clock=lambda: current[0],
        config=config,
    )
    request = create_request(manager)
    try:
        creating = await admitted_create(manager, request)
        assert creating["stage_timings_ms"] == {}
        session = await settled(manager, request["session_id"])
        status = await manager.control("status", request, project())
        assert status["stage_timings_ms"] == {"ssh_connect": 125, "pty_open": 250}
        assert (status["agent_version"], status["protocol_version"]) == (VERSION, 1)
        assert status["backpressure_count"] == status["backpressure_duration_ms"] == 0
        assert status["ring_bytes"] == 0 and "output_outstanding_bytes" not in status
        assert request["password"] not in repr(status) and request["username"] not in repr(status)
        status["stage_timings_ms"]["ssh_connect"] = -1
        assert session.public()["stage_timings_ms"]["ssh_connect"] == 125

        attach = attach_request(manager, request)
        await manager.control("attach", attach, project())
        transport.on_data(b"x" * (config.ring_bytes - config.ssh_window_bytes))
        current[0] += 0.25
        transport.on_data(b"x")
        assert transport.paused
        snapshot = await manager.control("status", request, project())
        assert (snapshot["backpressure_count"], snapshot["backpressure_duration_ms"]) == (1, 250)
        assert snapshot["ring_bytes"] == config.ring_bytes - config.ssh_window_bytes + 1
        assert snapshot["output_outstanding_bytes"] == session.credit.in_flight
        current[0] += 0.25
        if release == "ack":
            for _ in range(100):
                await asyncio.sleep(0)
                await manager.acknowledge({**attach, "rendered_offset": str(session.send_offset)})
                if not transport.paused:
                    break
            assert not transport.paused
        elif release == "exit":
            transport.on_exit(
                {
                    "exit_status": 0,
                    "exit_signal": None,
                    "output_complete": False,
                    "reason_code": "OUTPUT_INCOMPLETE",
                }
            )
        else:
            await manager.control(release, attach, project())
        current[0] += 1
        snapshot = await manager.control("status", request, project())
        assert (snapshot["backpressure_count"], snapshot["backpressure_duration_ms"]) == (1, 500)
        assert session.output_paused_at is None

        # The richest tmux end-state still fits the actual encrypted channel's
        # 32-field envelope limit after adding these five safe metrics fields.
        await manager.close_all()
        session.mode = "tmux"
        session.ended.update(exit_status=0, exit_signal=None, remote_end_confirmed=True)
        envelope = {"type": "terminal_state", **session.public()}
        queue = TerminalSendQueue()
        await queue.put_control(envelope)
        assert await queue.get() == envelope
        queue.close()
    finally:
        await manager.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["ssh_connect", "pty_open"])
async def test_diagnostics_include_failed_await_without_inventing_unrun_stage(failed_stage):
    current = [0.0]

    class Failure(FakeTransport):
        async def connect(self, target, password, *, challenge=None):
            current[0] += 0.125
            if failed_stage == "ssh_connect":
                raise SSHTransportError("SSH_AUTH_FAILED", "fixture authentication failed")
            return await super().connect(target, password, challenge=challenge)

        async def open_pty(self, *args, **kwargs):
            current[0] += 0.25
            raise SSHTransportError("SSH_CHANNEL_CLOSED", "fixture PTY rejected")

    manager = TerminalSessionManager(
        emit=lambda _: None, connector=Failure, authorize=lambda _: None, clock=lambda: current[0]
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        expected = {"ssh_connect": 125}
        if failed_stage == "pty_open":
            expected["pty_open"] = 250
        assert session.state == "failed"
        assert (await manager.control("status", request, project()))["stage_timings_ms"] == expected
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_diagnostics_pause_clock_zero_and_counters_saturate():
    current = [0.0]
    transport = FakeTransport()
    manager = TerminalSessionManager(
        emit=lambda _: None,
        connector=lambda: transport,
        authorize=lambda _: None,
        clock=lambda: current[0],
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attach = attach_request(manager, request)
        await manager.control("attach", attach, project())
        session.backpressure_count = (1 << 31) - 1
        session.backpressure_duration_ms = (1 << 31) - 2
        transport.on_data(b"x" * (manager.config.ring_bytes - manager.config.ssh_window_bytes))
        current[0] = 0.5
        transport.on_data(b"x")
        assert session.output_paused_at == 0.0
        status = await manager.control("status", request, project())
        assert status["backpressure_count"] == status["backpressure_duration_ms"] == (1 << 31) - 1
        await manager.control("detach", attach, project())
        assert session.backpressure_duration_ms == (1 << 31) - 1
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_tmux_lease_recovery_restarts_io_without_creating_another_shell():
    current, remote, transports, events = [0.0], {}, [], []

    def connector():
        transport = FakeTmuxTransport(remote)
        transports.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=events.append, connector=connector, authorize=lambda _: None, clock=lambda: current[0]
    )
    request = create_request(manager, mode="auto")
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attachment = attach_request(manager, request)
        await manager.control("attach", attachment, project())
        current[0] = manager.config.lease_seconds + 1
        await manager.sweep()
        assert session.stopped.is_set() and session.transport is None
        assert session.tmux is None
        assert session.public()["tmux_ref"] == TmuxReference.for_session(session.id).public()
        assert session.public()["recoverability"] == "reauthenticate"
        with pytest.raises(TerminalProtocolError):
            await manager.control("renew", request, project())
        await manager.control(
            "recover", {**request, "authority_revision": "fresh-recovery"}, project()
        )
        await settled(manager, request["session_id"])
        attachment.update(
            stream_epoch=str(session.stream_epoch), writer_epoch="3", attachment_id=uuid.uuid4().hex
        )
        await manager.control("attach", attachment, project())
        result = await manager.input(
            TerminalFrame(
                FrameKind.INPUT,
                session.id,
                manager.generation,
                session.stream_epoch,
                3,
                1,
                b"pwd\r",
            )
        )
        transports[-1].on_data(b"resumed\r\n")
        for _ in range(4):
            await asyncio.sleep(0)
        assert result["status"] == "forwarded"
        assert any(item.get("payload") == b"resumed\r\n" for item in events)
        assert remote["creates"] == 1
        assert not session.pump_task.done() and not session.input_task.done()
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_tmux_reauthorization_requires_new_revision_and_never_resurrects_closed_session():
    remote = {}
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: FakeTmuxTransport(remote), authorize=lambda _: None
    )
    request = create_request(manager, mode="tmux")
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        await manager.control("revoke", request, project())
        assert session.frozen and remote["exists"]
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control("recover", request, project())
        assert error.value.code == "AUTH_RECONFIRM_REQUIRED"
        with pytest.raises(TerminalProtocolError):
            await manager.control(
                "renew", {**request, "authority_revision": "new-login"}, project()
            )
        await manager.control("recover", {**request, "authority_revision": "new-login"}, project())
        await settled(manager, request["session_id"])
        assert not session.frozen and remote["creates"] == 1
        await manager.control("close", request, project())
        assert not remote["exists"]
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control(
                "recover", {**request, "authority_revision": "another-login"}, project()
            )
        assert error.value.code == "SESSION_ENDED"
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_recover_cannot_overwrite_revocation_while_old_transport_close_is_waiting():
    remote, closing, release = {}, asyncio.Event(), asyncio.Event()
    transports = []

    class Delayed(FakeTmuxTransport):
        async def close(self):
            self.closed = True
            closing.set()
            await release.wait()

    def connector():
        transport = Delayed(remote) if not transports else FakeTmuxTransport(remote)
        transports.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=lambda _: None, connector=connector, authorize=lambda _: None
    )
    request = create_request(manager, mode="tmux")
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        transports[0].closed = True  # The underlying TCP connection was lost.
        recovery = asyncio.create_task(
            manager.control("recover", {**request, "authority_revision": "new-auth"}, project())
        )
        await asyncio.wait_for(closing.wait(), 1)
        revoked = asyncio.create_task(manager.control("revoke", request, project()))
        for _ in range(3):
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(TerminalProtocolError) as error:
            await recovery
        assert error.value.code == "AUTH_REVOKED"
        await revoked
        assert session.frozen and session.stopped.is_set()
        assert len(transports) == 1 and remote["creates"] == 1
    finally:
        release.set()
        await manager.close_all()


@pytest.mark.asyncio
async def test_cancelled_close_receipt_does_not_cancel_bounded_cleanup():
    closing, release = asyncio.Event(), asyncio.Event()

    class Delayed(FakeTransport):
        async def close(self):
            closing.set()
            await release.wait()
            self.closed = True

    transport = Delayed()
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: transport, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        request_task = asyncio.create_task(manager.control("close", request, project()))
        await asyncio.wait_for(closing.wait(), 1)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        release.set()
        await asyncio.wait_for(asyncio.shield(session.finish_task), 1)
        assert transport.closed and session.transport is None and session.stopped.is_set()
    finally:
        release.set()
        await manager.close_all()


@pytest.mark.asyncio
async def test_detached_tmux_close_requires_credentials_and_can_reconcile_unknown_result():
    current, remote = [0.0], {}
    manager = TerminalSessionManager(
        emit=lambda _: None,
        connector=lambda: FakeTmuxTransport(remote),
        authorize=lambda _: None,
        clock=lambda: current[0],
    )
    request = create_request(manager, mode="tmux")
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        current[0] += manager.config.lease_seconds + 1
        await manager.sweep()
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control(
                "close",
                {key: value for key, value in request.items() if key != "password"},
                project(),
            )
        assert error.value.code == "REMOTE_END_UNCONFIRMED" and remote["exists"]
        remote["uncertain_close"] = True
        with pytest.raises(TerminalProtocolError):
            await manager.control("close", request, project())
        assert session.state == "close_unknown"
        assert session.public()["reason_code"] == "REMOTE_END_UNCONFIRMED"
        assert session.ended.get("remote_end_confirmed") is not True
        result = await manager.control("close", request, project())
        assert result["state"] == "exited" and result["remote_end_confirmed"] is True
        assert remote["creates"] == 1
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_unknown_tmux_create_never_falls_back_to_direct_or_repeats_create():
    remote = {"uncertain_create": True}
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: FakeTmuxTransport(remote), authorize=lambda _: None
    )
    request = create_request(manager, mode="auto")
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        assert session.mode == "tmux" and session.state == "failed"
        assert session.reason == "SSH_CONTROL_UNCERTAIN"
        await admitted_create(manager, request)
        assert remote["creates"] == 1
        await manager.control(
            "recover", {**request, "authority_revision": "new-confirmation"}, project()
        )
        await settled(manager, request["session_id"])
        assert session.state == "ready" and remote["creates"] == 1
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_cross_generation_auth_failure_retains_original_tmux_recoverability():
    remote, transports = {"exists": True}, []

    class AuthFailure(FakeTmuxTransport):
        async def connect(self, target, password, *, challenge=None):
            raise SSHTransportError("SSH_AUTH_FAILED", "fixture credential unavailable")

    def connector():
        transport = AuthFailure(remote) if not transports else FakeTmuxTransport(remote)
        transports.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=lambda _: None, connector=connector, authorize=lambda _: None
    )
    request = create_request(manager, mode="tmux", recover=True)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        assert session.state == "failed" and session.mode == "tmux"
        assert session.public()["recoverability"] == "reauthenticate"
        await manager.control(
            "recover", {**request, "authority_revision": "fixed-credential"}, project()
        )
        await settled(manager, request["session_id"])
        assert session.state == "ready" and session.mode == "tmux"
        assert remote.get("creates", 0) == 0
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_close_during_tmux_reauthentication_reconciles_the_existing_remote_session():
    remote, transports, connecting = {"exists": True}, [], asyncio.Event()

    class Connecting(FakeTmuxTransport):
        async def connect(self, target, password, *, challenge=None):
            connecting.set()
            await asyncio.Event().wait()

    def connector():
        transport = Connecting(remote) if not transports else FakeTmuxTransport(remote)
        transports.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=lambda _: None, connector=connector, authorize=lambda _: None
    )
    request = create_request(manager, mode="tmux", recover=True)
    try:
        await admitted_create(manager, request)
        await asyncio.wait_for(connecting.wait(), 1)
        result = await asyncio.wait_for(manager.control("close", request, project()), 1)
        assert result["remote_end_confirmed"] is True and result["state"] == "exited"
        assert not remote["exists"] and remote.get("creates", 0) == 0
        assert len(transports) == 2 and all(item.closed for item in transports)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_uncertain_persistent_creation_keeps_its_session_reservation():
    remote = {"uncertain_create": True}
    config = replace(DEFAULT_CONFIG, max_sessions_per_agent=1, max_sessions_per_user=1)
    manager = TerminalSessionManager(
        emit=lambda _: None,
        config=config,
        connector=lambda: FakeTmuxTransport(remote),
        authorize=lambda _: None,
    )
    request = create_request(manager, mode="auto")
    try:
        await admitted_create(manager, request)
        await settled(manager, request["session_id"])
        with pytest.raises(TerminalProtocolError) as error:
            await admitted_create(manager, create_request(manager))
        assert error.value.code == "SESSION_LIMIT"
        await manager.control("recover", {**request, "authority_revision": "fresh"}, project())
        session = await settled(manager, request["session_id"])
        assert session.state == "ready" and remote["creates"] == 1
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_late_old_writer_error_does_not_freeze_new_writer_tracker():
    transport, wake = FakeTransport(), asyncio.Event()
    waits = [0]

    async def wait_writable():
        waits[0] += 1
        if waits[0] == 1:
            await wake.wait()
            raise TimeoutError("old writer")

    transport.wait_writable = wait_writable
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: transport, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attachment = attach_request(manager, request)
        await manager.control("attach", attachment, project())
        old = asyncio.create_task(
            manager.input(
                TerminalFrame(FrameKind.INPUT, session.id, manager.generation, 1, 1, 1, b"old\r")
            )
        )
        for _ in range(3):
            await asyncio.sleep(0)
        tracker = session.tracker
        await manager.control(
            "attach",
            {**attachment, "attachment_id": uuid.uuid4().hex, "writer_epoch": "2"},
            project(),
        )
        wake.set()
        with pytest.raises(TerminalProtocolError):
            await old
        assert session.input_enabled and session.tracker is not tracker
        result = await manager.input(
            TerminalFrame(FrameKind.INPUT, session.id, manager.generation, 1, 2, 1, b"new\r")
        )
        assert result["writer_epoch"] == "2" and result["status"] == "forwarded"
        assert transport.writes == [b"old\r", b"new\r"]
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_closed_data_queue_detaches_and_new_attachment_resumes_output():
    events, fail = [], [True]

    def emit(event):
        if event["type"] == "terminal_output" and fail[0]:
            raise TerminalProtocolError("TERMINAL_QUEUE_CLOSED", "fixture queue closed")
        events.append(event)

    transport = FakeTransport()
    manager = TerminalSessionManager(
        emit=emit, connector=lambda: transport, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attachment = attach_request(manager, request)
        await manager.control("attach", attachment, project())
        transport.on_data(b"retained-output")
        for _ in range(3):
            await asyncio.sleep(0)
        assert not session.attached and not session.pump_task.done()
        fail[0] = False
        await manager.control(
            "attach",
            {**attachment, "attachment_id": uuid.uuid4().hex, "writer_epoch": "2"},
            project(),
        )
        for _ in range(3):
            await asyncio.sleep(0)
        assert (
            b"".join(item["payload"] for item in events if item["type"] == "terminal_output")
            == b"retained-output"
        )
    finally:
        await manager.close_all()


@pytest.mark.parametrize(
    "line",
    [
        "Include=custom.conf",
        "Include = custom.conf",
        "Include =custom.conf",
        "Include= custom.conf",
    ],
)
def test_native_include_equals_cannot_hide_custom_trust(tmp_path, line):
    directory = tmp_path / ".ssh"
    directory.mkdir()
    (directory / "config").write_text(line + "\n")
    (directory / "custom.conf").write_text("UserKnownHostsFile /some/other/known_hosts\n")
    with pytest.raises(HostTrustError) as error:
        check_native_trust_configuration(tmp_path)
    assert error.value.code == "HOST_TRUST_CONFIGURATION"


@pytest.mark.asyncio
async def test_same_create_after_resize_does_not_open_second_shell():
    transports, events = [], []

    def connector():
        transport = FakeTransport()
        transports.append(transport)
        return transport

    manager = TerminalSessionManager(
        emit=events.append, connector=connector, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        result = await admitted_create(manager, request)
        assert result["state"] == "creating"
        session = await settled(manager, request["session_id"])
        attach = attach_request(manager, request)
        await manager.control("attach", attach, project())
        await manager.control(
            "resize", {**attach, "resize_seq": "1", "cols": 150, "rows": 40}, project()
        )
        await admitted_create(manager, request)
        assert len(transports) == 1
        assert session.target["cols"] == 150
        assert "password" not in repr(session.target)
        assert request["password"] not in repr(events)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_inputs_at_most_once_and_old_writer_fenced():
    transport = FakeTransport()
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: transport, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        await settled(manager, request["session_id"])
        attach = attach_request(manager, request)
        await manager.control("attach", attach, project())
        frame = TerminalFrame(
            FrameKind.INPUT, request["session_id"], manager.generation, 1, 1, 1, b"whoami\r"
        )
        assert (await manager.input(frame))["status"] == "forwarded"
        assert (await manager.input(frame))["status"] == "forwarded"
        assert transport.writes == [b"whoami\r"]
        assert (
            manager.sessions[request["session_id"]].input_task.get_coro().cr_frame.f_locals["frame"]
            is None
        )
        await manager.control(
            "attach", {**attach, "attachment_id": uuid.uuid4().hex, "writer_epoch": "2"}, project()
        )
        with pytest.raises(TerminalProtocolError, match="不可输入|接管"):
            await manager.input(frame)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_revocation_does_not_wait_for_blocked_ssh_write():
    transport = FakeTransport()
    transport.block_write = asyncio.Event()
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: transport, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attach = attach_request(manager, request)
        await manager.control("attach", attach, project())
        task = asyncio.create_task(
            manager.input(
                TerminalFrame(
                    FrameKind.INPUT, request["session_id"], manager.generation, 1, 1, 1, b"secret\r"
                )
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.wait_for(manager.control("revoke", request, project()), 0.5)
        with pytest.raises(TerminalProtocolError):
            await task
        assert session.frozen and transport.closed
        assert session.ring.capacity_bytes == 1
        assert "secret" not in repr(session.tracker._receipts)
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_output_credit_blocks_until_consumption_ack_and_fences_old_attachment():
    events = []
    transport = FakeTransport()
    manager = TerminalSessionManager(
        emit=events.append, connector=lambda: transport, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attach = attach_request(manager, request)
        await manager.control("attach", attach, project())
        data = b"x" * (manager.config.output_high_water_bytes + 100)
        transport.on_data(data)
        for _ in range(30):
            await asyncio.sleep(0)
        output = [e for e in events if e["type"] == "terminal_output"]
        assert sum(len(e["payload"]) for e in output) == manager.config.output_high_water_bytes
        assert not (
            await manager.acknowledge(
                {
                    **attach,
                    "attachment_id": uuid.uuid4().hex,
                    "rendered_offset": str(manager.config.output_high_water_bytes),
                }
            )
        )["accepted"]
        await manager.acknowledge(
            {**attach, "rendered_offset": str(manager.config.output_high_water_bytes)}
        )
        for _ in range(4):
            await asyncio.sleep(0)
        assert sum(len(e["payload"]) for e in events if e["type"] == "terminal_output") == len(data)
        assert session.credit.in_flight == 100
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_direct_new_renderer_after_ring_gap_is_rejected():
    transport = FakeTransport()
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=lambda: transport, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        await settled(manager, request["session_id"])
        attach = attach_request(manager, request)
        await manager.control("attach", attach, project())
        await manager.control("detach", attach, project())
        transport.on_data(b"x" * (manager.config.ring_bytes + 1))
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control(
                "attach", attach_request(manager, request, writer_epoch="2"), project()
            )
        assert error.value.code == "HISTORY_GAP"
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_lease_expiry_closes_direct_without_browser_cleanup():
    current = [0.0]
    transport = FakeTransport()
    manager = TerminalSessionManager(
        emit=lambda _: None,
        connector=lambda: transport,
        authorize=lambda _: None,
        clock=lambda: current[0],
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        current[0] = manager.config.lease_seconds
        await manager.sweep()
        assert transport.closed
        assert session.reason == "AUTH_LEASE_EXPIRED"
        with pytest.raises(TerminalProtocolError):
            await manager.control("renew", request, project())
    finally:
        await manager.close_all()


def test_custom_native_trust_is_rejected_without_running_ssh(tmp_path):
    directory = tmp_path / ".ssh"
    directory.mkdir()
    (directory / "config").write_text("Host *\n  HostKeyAlias production\n")
    with pytest.raises(HostTrustError) as error:
        check_native_trust_configuration(tmp_path)
    assert error.value.code == "HOST_TRUST_CONFIGURATION"


def test_accept_new_rechecks_concurrent_first_key_and_keeps_existing_content(tmp_path):
    ssh = pytest.importorskip("asyncssh")
    path = tmp_path / "known_hosts"
    path.write_text("# existing comment\n")
    one = HostKeyPolicy(ssh, "accept-new", sources=[path])
    two = HostKeyPolicy(ssh, "accept-new", sources=[path])
    key1, key2 = (
        ssh.import_public_key(ssh.generate_private_key("ssh-ed25519").export_public_key())
        for _ in range(2)
    )
    assert one.validate_candidate("server", "127.0.0.1", 2222, key1)
    assert two.validate_candidate("server", "127.0.0.1", 2222, key2)
    one.commit_before_authentication()
    with pytest.raises(HostTrustError) as error:
        two.commit_before_authentication()
    assert error.value.code == "HOST_KEY_CHANGED"
    assert path.read_text().startswith("# existing comment\n[server]:2222 ")
    strict = HostKeyPolicy(ssh, "strict", sources=[path])
    assert strict.validate_candidate("server", "127.0.0.1", 2222, key1)
    assert not strict.validate_candidate("server", "127.0.0.1", 22, key1)


def test_hashed_host_revocation_and_no_nondefault_port_fallback(tmp_path):
    import base64
    import hmac

    ssh = pytest.importorskip("asyncssh")
    key = ssh.import_public_key(ssh.generate_private_key("ssh-ed25519").export_public_key())
    public = key.export_public_key().decode().strip()
    salt = b"s" * 20
    hashed = (
        "|1|"
        + base64.b64encode(salt).decode()
        + "|"
        + base64.b64encode(hmac.new(salt, b"server", hashlib.sha1).digest()).decode()
    )
    path = tmp_path / "known_hosts"
    path.write_text(hashed + " " + public + "\n")
    policy = HostKeyPolicy(ssh, "strict", sources=[path])
    assert policy.validate_candidate("server", "127.0.0.1", 22, key)
    assert not policy.validate_candidate("server", "127.0.0.1", 2222, key)
    path.write_text("@revoked server " + public + "\n")
    assert not policy.validate_candidate("server", "127.0.0.1", 22, key)
    assert policy.error.code == "HOST_KEY_REVOKED"


@pytest.mark.asyncio
async def test_real_asyncssh_password_pty_resize_binary_and_eof(tmp_path):
    ssh = pytest.importorskip("asyncssh")
    seen = {"passwords": [], "sizes": []}

    class Echo(ssh.SSHServerSession):
        def connection_made(self, channel):
            self.channel = channel

        def pty_requested(self, term, size, modes):
            seen["term"] = term
            seen["sizes"].append(size[:2])
            return True

        def shell_requested(self):
            return True

        def session_started(self):
            self.channel.write(b"ready\r\n")

        def terminal_size_changed(self, width, height, pixwidth, pixheight):
            seen["sizes"].append((width, height))

        def data_received(self, data, datatype):
            if data == b"exit\r":
                self.channel.write(b"sentinel-final\r\n")
                self.channel.exit(0)
            else:
                self.channel.write(data)

    class Server(ssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            seen["passwords"].append(password)
            # TOFU must be committed before any password reaches the peer.
            assert (tmp_path / "known_hosts").exists()
            return password == "fixture-password"

        def session_requested(self):
            return Echo()

    host_key = ssh.generate_private_key("ssh-ed25519")
    server = await ssh.create_server(
        Server, "127.0.0.1", 0, server_host_keys=[host_key], encoding=None, line_editor=False
    )
    transport = AsyncSSHTransport(DEFAULT_CONFIG, trust_sources=[tmp_path / "known_hosts"])
    output, finished = [], asyncio.get_running_loop().create_future()
    try:
        await transport.connect(
            {
                "host": "127.0.0.1",
                "port": server.get_port(),
                "username": "fixture",
                "host_key_policy": "accept-new",
            },
            "fixture-password",
        )
        await transport.open_pty(120, 30, output.append, lambda event: finished.set_result(event))
        transport.resize(90, 25)
        transport.write("中文".encode() + b"\x00\xff")
        await transport.wait_writable()
        await asyncio.sleep(0.02)
        transport.write(b"exit\r")
        result = await asyncio.wait_for(finished, 2)
        assert seen["passwords"] == ["fixture-password"]
        assert seen["term"] == "xterm-256color"
        assert (90, 25) in seen["sizes"]
        assert b"\x00\xff" in b"".join(output)
        assert b"".join(output).endswith(b"sentinel-final\r\n")
        assert result["exit_status"] == 0 and result["output_complete"]
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_real_unknown_host_never_receives_password(tmp_path):
    ssh = pytest.importorskip("asyncssh")
    passwords = []

    class Server(ssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            passwords.append(password)
            return True

    server = await ssh.create_server(
        Server, "127.0.0.1", 0, server_host_keys=[ssh.generate_private_key("ssh-ed25519")]
    )
    transport = AsyncSSHTransport(DEFAULT_CONFIG, trust_sources=[tmp_path / "known_hosts"])
    try:
        with pytest.raises((SSHTransportError, HostTrustError)) as error:
            await transport.connect(
                {
                    "host": "127.0.0.1",
                    "port": server.get_port(),
                    "username": "fixture",
                    "host_key_policy": "strict",
                },
                "sensitive-fixture",
            )
        assert error.value.code == "HOST_KEY_UNKNOWN"
        assert passwords == []
        assert not (tmp_path / "known_hosts").exists()
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_real_keyboard_interactive_challenge_is_bounded_and_answered_in_memory(tmp_path):
    ssh = pytest.importorskip("asyncssh")
    received, challenges = [], []

    class Server(ssh.SSHServer):
        def begin_auth(self, username):
            return True

        def kbdint_auth_supported(self):
            return True

        def get_kbdint_challenge(self, username, lang, submethods):
            return "Verification", "Enter a one-time code", "", [("Verification code: ", False)]

        def validate_kbdint_response(self, username, responses):
            received.append(tuple(responses))
            return responses == ["123456"]

    async def challenge(name, instruction, prompts):
        challenges.append((name, instruction, prompts))
        return ["123456"]

    server = await ssh.create_server(
        Server, "127.0.0.1", 0, server_host_keys=[ssh.generate_private_key("ssh-ed25519")]
    )
    transport = AsyncSSHTransport(DEFAULT_CONFIG, trust_sources=[tmp_path / "known_hosts"])
    try:
        await transport.connect(
            {
                "host": "127.0.0.1",
                "port": server.get_port(),
                "username": "fixture",
                "host_key_policy": "accept-new",
            },
            "saved-password",
            challenge=challenge,
        )
        assert transport.connected
        assert received == [("123456",)]
        assert len(challenges) == 1 and challenges[0][2] == [("Verification code: ", False)]
        assert "123456" not in repr(challenges) and "saved-password" not in repr(challenges)
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_manager_auth_answer_obeys_revocation_and_does_not_leak_responses():
    events, answered = [], []

    class Interactive(FakeTransport):
        async def connect(self, target, password, *, challenge=None):
            response = await challenge("Login", "OTP required", [("Code: ", False)])
            answered.append(response)
            return self

    manager = TerminalSessionManager(
        emit=events.append, connector=Interactive, authorize=lambda _: None
    )
    request = create_request(manager)
    try:
        await admitted_create(manager, request)
        for _ in range(4):
            await asyncio.sleep(0)
        event = next(item for item in events if item["type"] == "terminal_auth_challenge")
        assert event["deadline_seconds"] <= 120
        await manager.control(
            "auth_answer",
            {**request, "challenge_id": event["challenge_id"], "responses": ["654321"]},
            project(),
        )
        session = await settled(manager, request["session_id"])
        assert answered == [["654321"]] and session.challenge is None
        assert "654321" not in repr(events) and "654321" not in repr(session.target)
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control(
                "auth_answer",
                {**request, "challenge_id": event["challenge_id"], "responses": ["654321"]},
                project(),
            )
        assert error.value.code == "AUTH_CHALLENGE_EXPIRED"
        await manager.control("revoke", request, project())
        with pytest.raises(TerminalProtocolError) as error:
            await manager.control(
                "auth_answer",
                {**request, "challenge_id": event["challenge_id"], "responses": ["654321"]},
                project(),
            )
        assert error.value.code == "AUTH_REVOKED"
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_real_100_mib_stream_has_bounded_buffers_and_final_sentinel(tmp_path):
    ssh = pytest.importorskip("asyncssh")
    config = replace(
        DEFAULT_CONFIG,
        ring_bytes=65536,
        ssh_window_bytes=16384,
        output_high_water_bytes=32768,
        output_low_water_bytes=8192,
        lease_seconds=120,
    )
    chunk = bytes(range(256)) * 64
    rounds = 100 * 1024 * 1024 // len(chunk)
    sentinel = b"\r\ncodepier-final-sentinel\r\n"
    expected = hashlib.sha256()
    for _ in range(rounds):
        expected.update(chunk)
    expected.update(sentinel)
    producer_tasks, pauses = [], [0]

    class Stream(ssh.SSHServerSession):
        def connection_made(self, channel):
            self.channel = channel
            channel.set_write_buffer_limits(high=32768, low=8192)
            self.writable = asyncio.Event()
            self.writable.set()
            self.started = False

        def pty_requested(self, term, size, modes):
            return True

        def shell_requested(self):
            return True

        def pause_writing(self):
            pauses[0] += 1
            self.writable.clear()

        def resume_writing(self):
            self.writable.set()

        def connection_lost(self, exc):
            self.writable.set()

        def data_received(self, data, datatype):
            if self.started:
                return
            self.started = True
            producer_tasks.append(asyncio.create_task(self.produce()))

        async def produce(self):
            for _ in range(rounds):
                self.channel.write(chunk)
                await self.writable.wait()
            self.channel.write(sentinel)
            self.channel.write_eof()
            self.channel.exit(0)

    class Server(ssh.SSHServer):
        def begin_auth(self, username):
            return False

        def session_requested(self):
            return Stream()

    server = await ssh.create_server(
        Server,
        "127.0.0.1",
        0,
        encoding=None,
        line_editor=False,
        server_host_keys=[ssh.generate_private_key("ssh-ed25519")],
    )
    actual, total, max_ring = hashlib.sha256(), [0], [0]
    finished = asyncio.get_running_loop().create_future()
    attachment = None
    request = None

    async def consume(event):
        if event["type"] == "terminal_output":
            actual.update(event["payload"])
            total[0] += len(event["payload"])
            max_ring[0] = max(max_ring[0], manager.sessions[request["session_id"]].ring.size)
            # Make the downstream consumer slower than the peer. Credit is
            # returned only after this simulated terminal rendering completes.
            await asyncio.sleep(0.0001)
            await manager.acknowledge(
                {**attachment, "rendered_offset": str(int(event["offset"]) + len(event["payload"]))}
            )
        elif event["type"] == "terminal_exit" and not finished.done():
            finished.set_result(event)

    manager = TerminalSessionManager(
        emit=consume,
        config=config,
        authorize=lambda _: None,
        connector=lambda: AsyncSSHTransport(config, trust_sources=[tmp_path / "known_hosts"]),
    )
    request = create_request(
        manager, host="127.0.0.1", port=server.get_port(), host_key_policy="accept-new"
    )
    try:
        await admitted_create(manager, request)
        session = await settled(manager, request["session_id"])
        attachment = attach_request(manager, request)
        await manager.control("attach", attachment, project())
        await manager.input(
            TerminalFrame(FrameKind.INPUT, session.id, manager.generation, 1, 1, 1, b"start\r")
        )
        event = await asyncio.wait_for(finished, 30)
        assert total[0] == 100 * 1024 * 1024 + len(sentinel)
        assert actual.digest() == expected.digest()
        assert max_ring[0] <= config.ring_bytes and session.credit.in_flight == 0
        assert pauses[0] > 0 and event["final_offset"] == str(total[0])
        assert event["attachment_exit_status"] == 0
        assert event["output_complete"] is True
    finally:
        await manager.close_all()
        for task in producer_tasks:
            task.cancel()
        await asyncio.gather(*producer_tasks, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_tmux_create_recover_close_never_creates_during_recovery(tmp_path, monkeypatch):
    import socket
    import tempfile

    binary = shutil.which("tmux")
    if not binary:
        pytest.skip("An isolated tmux binary is required")
    short_directory = tempfile.TemporaryDirectory(prefix="cpt-")
    monkeypatch.setenv("TMUX_TMPDIR", short_directory.name)
    try:
        with socket.socket(socket.AF_UNIX) as probe:
            probe.bind(short_directory.name + "/probe")
            probe.listen()
    except PermissionError:
        short_directory.cleanup()
        pytest.skip("Execution environment denies Unix-domain sockets required by tmux")

    class LocalControl:
        async def run_control(self, command, **kwargs):
            process = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            output, _ = await asyncio.wait_for(process.communicate(), 5)
            return process.returncode, output

    controller = TmuxController(LocalControl(), uuid.uuid4().hex, DEFAULT_CONFIG)
    assert (await controller.probe())["available"]
    try:
        with pytest.raises(SSHTransportError) as error:
            await controller.recover()
        assert error.value.code == "REMOTE_SESSION_MISSING"
        await controller.create(90, 25)
        assert await controller.exists()
        await controller.recover()
        code, output = await controller.transport.run_control(
            controller.reference.prefix()
            + " display-message -p -t "
            + controller.reference.session
            + " '#{history_limit}'"
        )
        assert code == 0 and output.strip() == str(DEFAULT_CONFIG.tmux_history_lines).encode()
        await controller.close()
        assert not await controller.exists()
        with pytest.raises(SSHTransportError):
            await controller.recover()
        assert "new-session" not in controller.attach_command()
    finally:
        await controller.close()
        short_directory.cleanup()


@pytest.mark.parametrize(
    "malformed", ["server ssh-ed25519", "@revoked {bare}", "ssh-ed25519 {invalid}"]
)
def test_malformed_scoped_or_noncanonical_known_hosts_records_fail_closed(tmp_path, malformed):
    ssh = pytest.importorskip("asyncssh")
    bare = ssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
    path = tmp_path / "known_hosts"
    path.write_text(malformed.format(bare=bare, invalid="A" * 68) + "\n")
    with pytest.raises(HostTrustError) as error:
        HostKeyPolicy(ssh, "accept-new", sources=[path])
    assert error.value.code == "HOST_TRUST_CONFIGURATION"


def test_canonical_bare_ed25519_rows_do_not_grant_or_block_host_trust(tmp_path):
    ssh = pytest.importorskip("asyncssh")
    key = ssh.generate_private_key("ssh-ed25519").convert_to_public()
    other = ssh.generate_private_key("ssh-ed25519").convert_to_public()
    public = key.export_public_key().decode().strip()
    bare = other.export_public_key().decode().strip()
    data = f"server {public}\n{bare}\n@revoked revoked {public}\n"
    path = tmp_path / "known_hosts"
    path.write_text(data)
    policy = HostKeyPolicy(ssh, "strict", sources=[path])
    assert policy.validate_candidate("server", "", 22, key)
    assert not policy.validate_candidate("server", "", 22, other)
    assert policy.error.code == "HOST_KEY_CHANGED"
    assert not policy.validate_candidate("new-host", "", 22, other)
    assert policy.error.code == "HOST_KEY_UNKNOWN"
    assert not policy.validate_candidate("revoked", "", 22, key)
    assert policy.error.code == "HOST_KEY_REVOKED"
    assert path.read_text() == data


def test_direct_vps_denial_and_link_hello_capabilities():
    from agent.shell import direct_vps_denial
    from agent.ssh_terminal_link import AgentTerminalLink

    assert direct_vps_denial({}) == (
        "SHELL_DISABLED",
        "请在本机 Agent 配置中开启 shell.enabled 执行权限",
    )
    assert direct_vps_denial({"shell": {"enabled": True, "projects": ["Alpha"]}}) == (
        "SHELL_NOT_ALLOWED",
        "无项目直连 VPS 需要本机 shell.projects 包含 * 授权",
    )
    assert direct_vps_denial({"shell": {"enabled": True, "projects": ["*"]}}) is None

    default_hello = AgentTerminalLink(None)._hello()
    assert default_hello["direct_vps"] is True
    assert default_hello["direct_vps_allowed"] is True
    assert default_hello["probe"] is True

    denied_agent = SimpleNamespace(config={"shell": {"enabled": False, "projects": []}})
    assert AgentTerminalLink(denied_agent)._hello()["direct_vps_allowed"] is False

    allowed_agent = SimpleNamespace(config={"shell": {"enabled": True, "projects": ["*"]}})
    assert AgentTerminalLink(allowed_agent)._hello()["direct_vps_allowed"] is True


@pytest.mark.asyncio
async def test_manager_direct_vps_gate_and_cross_mode_isolation():
    agent_stub = SimpleNamespace(config={"shell": {"enabled": False, "projects": []}})
    manager = TerminalSessionManager(agent=agent_stub, emit=lambda _: None, connector=FakeTransport)
    direct = {
        "_direct_vps": True,
        "id": "",
        "root": "",
        "_terminal_owner": "user:owner-a",
        "_terminal_space": "space-a",
    }
    try:
        with pytest.raises(TerminalProtocolError) as bad_path:
            await manager.control("list", {}, {**direct, "root": "/not-allowed"})
        assert bad_path.value.code == "AUTH_REVOKED"

        with pytest.raises(TerminalProtocolError) as disabled:
            await manager.control("list", {}, direct)
        assert disabled.value.code == "SHELL_DISABLED"

        agent_stub.config = {"shell": {"enabled": True, "projects": ["project-a"]}}
        with pytest.raises(TerminalProtocolError) as not_wildcard:
            await manager.control("list", {}, direct)
        assert not_wildcard.value.code == "SHELL_NOT_ALLOWED"

        agent_stub.config = {"shell": {"enabled": True, "projects": ["*"]}}
        assert manager._scope("owner-a", "space-a", direct) == ("owner-a", "space-a", "", "")
        req = create_request(manager, binding_id="direct:vps-a")
        await admitted_create(manager, req, direct)
        await settled(manager, req["session_id"])

        isolated_mgr = TerminalSessionManager(
            emit=lambda _: None, connector=FakeTransport, authorize=lambda _: None
        )
        isolated_mgr.sessions.update(manager.sessions)
        non_direct = {
            "id": "",
            "root": "",
            "_terminal_owner": "user:owner-a",
            "_terminal_space": "space-a",
        }
        with pytest.raises(TerminalProtocolError) as cross:
            await isolated_mgr.control("status", {"session_id": req["session_id"]}, non_direct)
        assert cross.value.code == "SESSION_NOT_FOUND"
        assert (await isolated_mgr.control("list", {}, non_direct))["sessions"] == []
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_manager_stateless_probe_redaction_and_error_classification():
    calls = []

    class ProbeTransport(FakeTransport):
        def __init__(self, fail=False):
            super().__init__()
            self.fail = fail

        async def connect(self, target, password, *, challenge=None):
            assert challenge is None
            calls.append(("connect", dict(target), password))
            if self.fail:
                raise SSHTransportError("SSH_AUTH_FAILED", f"rejected {password}")
            return self

        async def run_control(self, command, *, wait_seconds=None, max_bytes=8192):
            calls.append(("run_control", command, wait_seconds, max_bytes))
            return 0, b"CODEPIER_VPS_OK\nhost-1\nfixture-secret-only\n"

    transports = []

    def make_ok():
        item = ProbeTransport(fail=False)
        transports.append(item)
        return item

    direct = {
        "_direct_vps": True,
        "id": "",
        "root": "",
        "_terminal_owner": "user:owner-a",
        "_terminal_space": "space-a",
    }
    manager = TerminalSessionManager(
        emit=lambda _: None, connector=make_ok, authorize=lambda _: None
    )
    payload = {
        "host": "server.example.invalid",
        "port": 2222,
        "username": "root",
        "password": "fixture-secret-only",
        "host_key_policy": "accept-new",
        "timeout_seconds": 99,
    }
    res = await manager.control("probe", payload, direct)
    assert res["ok"] is True and res["state"] == "succeeded" and res["exit_code"] == 0
    assert "CODEPIER_VPS_OK" in res["output"] and "<redacted>" in res["output"]
    assert "fixture-secret-only" not in res["output"] and "password" not in payload
    assert calls[1][2] == 60 and calls[1][3] == 16384
    assert transports[0].closed is True and manager.sessions == {}

    fail_mgr = TerminalSessionManager(
        emit=lambda _: None,
        connector=lambda: ProbeTransport(fail=True),
        authorize=lambda _: None,
    )
    failed = await fail_mgr.control("probe", {**payload, "password": "fixture-secret-only"}, direct)
    assert failed["ok"] is False and failed["state"] == "failed" and failed["exit_code"] is None
    assert failed["error"] == {"code": "SSH_AUTH_FAILED", "message": "rejected <redacted>"}
