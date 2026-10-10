"""Agent admission and durable receipt integration in disposable local projects."""
import asyncio
import time

import pytest

from agent.scheduler import Pressure
from tests.test_core_tools import agent, request


@pytest.mark.asyncio
async def test_project_b_starts_while_project_a_has_a_long_backlog(agent, monkeypatch):
    instance, project, _ = agent
    await instance.scheduler.reconfigure({"adaptive": False, "initial": 4})
    release = asyncio.Event()
    started = []
    changed = asyncio.Condition()

    async def execute(identifier, tool, current, args):
        async with changed:
            started.append((identifier, current["id"]))
            changed.notify_all()
        await release.wait()
        return {"exit_code": 0, "output": "fixture"}
    monkeypatch.setattr(instance, "execute", execute)
    calls = [request({**project, "id": "A"}, "exec", command="fixture", idempotency_key=f"fair-project-a-{i}") for i in range(8)]
    tasks = [asyncio.create_task(instance.handle(call)) for call in calls]
    try:
        async with asyncio.timeout(1):
            async with changed:
                await changed.wait_for(lambda: len(started) == 3)
        other = request({**project, "id": "B"}, "exec", command="fixture", idempotency_key="fair-project-b")
        tasks.append(asyncio.create_task(instance.handle(other)))
        async with asyncio.timeout(1):
            async with changed:
                await changed.wait_for(lambda: any(pid == "B" for _, pid in started))
        assert len(started) == 4
        assert instance.scheduler.snapshot("A")["queued"] == 5
        assert instance.scheduler.snapshot("B")["running"] == 1
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert all(instance.journal.status(call["id"])["result"]["ok"] for call in calls)
    assert not instance.scheduler.active and not instance.scheduler.waiting


@pytest.mark.asyncio
async def test_repeat_delivery_and_lost_receipt_do_not_duplicate_execution(agent, monkeypatch):
    instance, project, _ = agent
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    async def execute(*args):
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return {"exit_code": 0, "output": "once"}
    monkeypatch.setattr(instance, "execute", execute)
    call = request({**project, "id": "A"}, "exec", command="fixture", idempotency_key="receipt-once")
    original = asyncio.create_task(instance.handle(call))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await instance.handle(call)
        assert count == 1 and instance.scheduler.snapshot()["running"] == 1
    finally:
        release.set()
        await original
    first = instance.journal.status(call["id"])["result"]
    await instance.handle(call)
    assert instance.journal.status(call["id"])["result"] == first
    assert count == 1 and not instance.scheduler.active


@pytest.mark.asyncio
async def test_pressure_reduction_keeps_active_receipt_and_expires_queued_operation(agent, monkeypatch):
    instance, project, root = agent
    entered, release = asyncio.Event(), asyncio.Event()
    async def execute(*args):
        entered.set()
        await release.wait()
        return {"exit_code": 0, "output": "completed"}
    monkeypatch.setattr(instance, "execute", execute)
    call = request({**project, "id": "A"}, "exec", command="fixture", idempotency_key="pressure-long")
    original = asyncio.create_task(instance.handle(call))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await instance.scheduler.update_pressure(Pressure(time.monotonic(), None, 10, 1024**3))
        assert instance.scheduler.capacity.limit == 0
        other = request({**project, "id": "B"}, "exec", command="fixture", idempotency_key="pressure-expires")
        other["not_after"] = time.time() + .03
        await instance.handle(other)
        assert instance.journal.status(other["id"])["result"]["error"]["code"] == "QUEUE_EXPIRED"
        assert instance.journal.status(call["id"])["status"] == "running"
        assert instance.scheduler.snapshot()["queued"] == 0
    finally:
        release.set()
        await original
    assert instance.journal.status(call["id"])["result"]["ok"]


@pytest.mark.asyncio
async def test_invalid_reload_preserves_config_and_active_ownership(agent):
    instance, _, _ = agent
    previous = instance.scheduler.config
    async with instance.scheduler.slot("running", "A"):
        with pytest.raises(ValueError):
            await instance.scheduler.reconfigure({"maximum": -1})
        assert instance.scheduler.config is previous
        await instance.scheduler.reconfigure({"adaptive": False, "minimum": 1, "initial": 1, "maximum": 1})
        assert instance.scheduler.snapshot()["running"] == 1
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.01):
                async with instance.scheduler.slot("queued", "B"):
                    pytest.fail("must drain before another admission")
    assert not instance.scheduler.active


@pytest.mark.asyncio
async def test_project_diagnostics_do_not_expose_other_project_operations(agent):
    instance, project, _ = agent
    async with instance.scheduler.slot("private-other-operation", "private-project"):
        call = request({**project, "id": "A"}, "agent_diagnostics")
        await instance.handle(call)
        result = instance.journal.status(call["id"])["result"]
        assert result["ok"]
        assert result["data"]["scheduler"]["scope"] == "project"
        assert result["data"]["scheduler"]["running"] == 0
        assert "private-project" not in str(result)
        assert "private-other-operation" not in str(result)


def scheduler_packet(config):
    from hub.scheduler_settings import revision
    return {"type": "scheduler_config", "config": config, "revision": revision(config)}


@pytest.mark.asyncio
async def test_invalid_preference_preserves_active_limits_revision_and_recovery(agent):
    instance, _, _ = agent
    good = scheduler_packet({"maximum": 4})
    assert await instance.apply_scheduler_config(good)
    config = instance.scheduler.config
    instance.scheduler.capacity.healthy = 2
    changed = instance.scheduler.capacity.changed_at
    assert await instance.apply_scheduler_config(good)
    assert instance.scheduler.capacity.healthy == 2
    assert instance.scheduler.capacity.changed_at == changed
    async with instance.scheduler.slot("running", "A"):
        for bad in (None, {"revision": "0" * 64}, scheduler_packet({"maximum": -1}),
                    scheduler_packet({"project_limits": {str(i): 2 for i in range(257)}})):
            assert not await instance.apply_scheduler_config(bad)
            assert instance.scheduler.config is config
            assert instance.scheduler_revision == good["revision"]
            assert instance.scheduler_error == "SCHEDULER_SETTINGS_INVALID"
            assert instance.scheduler.snapshot()["running"] == 1
    assert await instance.apply_scheduler_config(good)
    assert instance.scheduler_error == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("handshake", [False, True])
async def test_invalid_preference_does_not_close_authenticated_transport(agent, monkeypatch, handshake):
    import json
    from shared.tool_protocol import advertisement

    instance, _, _ = agent
    good = scheduler_packet({"maximum": 4})
    assert await instance.apply_scheduler_config(good)
    bad = scheduler_packet({"maximum": -1})
    ready = {"type": "ready", **advertisement(instance.build.catalog_sha256), "scheduler_config": bad if handshake else good}
    frames = [{"type": "probe", "id": "probe-after-bad"}]
    if not handshake:
        frames.insert(0, bad)
    received = []

    class Socket:
        def __init__(self):
            self.initial = iter([json.dumps({"type": "challenge", "challenge": "fixture"}), ready])
            self.frames = iter(frames)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def recv(self):
            return next(self.initial)

        async def send(self, packet):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.frames)
            except StopIteration:
                raise StopAsyncIteration

    class Channel:
        def __init__(self, *args):
            pass

        def pack(self, body):
            return body

        def unpack(self, packet):
            return packet

    async def idle():
        await asyncio.Event().wait()

    async def status(identifier):
        received.append(identifier)

    monkeypatch.setattr("agent.runner.websockets.connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr("agent.runner.SecureChannel", Channel)
    monkeypatch.setattr(instance, "heartbeat", idle)
    monkeypatch.setattr(instance.native, "sync", idle)
    monkeypatch.setattr(instance, "report_status", status)
    await instance.connect_once()
    assert received == ["probe-after-bad"]
    assert instance.connection_number == 1
    assert instance.scheduler_revision == good["revision"]
    assert instance.scheduler_error == "SCHEDULER_SETTINGS_INVALID"


@pytest.mark.asyncio
async def test_agent_applies_disjoint_local_and_panel_project_caps(agent):
    instance, _, _ = agent
    instance.config["scheduler"]["project_limits"] = {f"local-{i}": 2 for i in range(256)}
    requested = {"project_limits": {f"panel-{i}": 6 for i in range(256)}}
    assert await instance.apply_scheduler_config(scheduler_packet(requested))
    assert len(instance.scheduler.config["project_limits"]) == 512
    assert instance.scheduler.config["project_limits"]["local-0"] == 2


def remote_call(project, tool, host, key):
    legacy = request(project, "ssh_exec", host=host, port=22, username="fixture",
                     password="test-only", command="fixture", idempotency_key=key)
    if tool == "ssh_exec":
        return legacy
    call = request(project, "exec", target="vps:" + "a" * 32,
                   command="fixture", idempotency_key=key)
    call["core_ssh"] = legacy["args"]
    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("first_tool", ["exec", "ssh_exec"])
@pytest.mark.parametrize("second_tool", ["exec", "ssh_exec"])
@pytest.mark.parametrize("hosts", [
    ("host.example", "HOST.EXAMPLE."),
    ("2001:db8::1", "2001:0db8:0000:0000:0000:0000:0000:0001"),
])
async def test_typed_remote_entries_share_canonical_target_cap(agent, monkeypatch, first_tool, second_tool, hosts):
    instance, project, root = agent
    await instance.scheduler.reconfigure({"adaptive": False, "initial": 4,
                                         "remote_limit": 4, "remote_target_limit": 1})
    calls = []
    for index, (tool, host) in enumerate(((first_tool, hosts[0]), (second_tool, hosts[1]),
                                         (second_tool, "other.example"))):
        directory = root / str(index)
        directory.mkdir()
        current = {**project, "id": str(index), "root": str(directory)}
        calls.append(remote_call(current, tool, host, "remote-cap-" + str(index)))
    entered = {call["id"]: asyncio.Event() for call in calls}
    release, waiting = asyncio.Event(), asyncio.Event()
    original_phase = instance.phase

    def phase(identifier, stage, **detail):
        original_phase(identifier, stage, **detail)
        if identifier == calls[1]["id"] and detail.get("queue_reason") == "remote_target_limit":
            waiting.set()

    async def execute(identifier, *args):
        entered[identifier].set()
        await release.wait()
        return {"exit_code": 0, "output": "fixture"}

    monkeypatch.setattr(instance, "phase", phase)
    monkeypatch.setattr(instance, "execute", execute)
    tasks = []
    try:
        tasks.append(asyncio.create_task(instance.handle(calls[0])))
        await asyncio.wait_for(entered[calls[0]["id"]].wait(), 1)
        tasks.append(asyncio.create_task(instance.handle(calls[1])))
        await asyncio.wait_for(waiting.wait(), 1)
        assert not entered[calls[1]["id"]].is_set()
        tasks.append(asyncio.create_task(instance.handle(calls[2])))
        await asyncio.wait_for(entered[calls[2]["id"]].wait(), 1)
        lanes = instance.scheduler.snapshot()["lanes"]
        assert lanes["remote"]["running"] == 2 and lanes["remote"]["queued"] == 1
        assert lanes["execution"]["running"] == 0
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert all(instance.journal.status(call["id"])["result"]["ok"] for call in calls)
    assert not instance.scheduler.active and not instance.scheduler.waiting


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["exec", "ssh_exec"])
async def test_typed_remote_entries_obey_remote_lane_limit(agent, monkeypatch, tool):
    instance, project, _ = agent
    await instance.scheduler.reconfigure({"remote_limit": 1})
    call = remote_call({**project, "id": "queued"}, tool, "other.example", "remote-lane-limit")
    call["not_after"] = time.time() + .03

    async def unexpected(*args):
        pytest.fail("a full remote lane must not execute another SSH operation")

    monkeypatch.setattr(instance, "execute", unexpected)
    async with instance.scheduler.slot("already-running", "other-project", "remote", "first.example:22"):
        await instance.handle(call)
        assert instance.journal.status(call["id"])["result"]["error"]["code"] == "QUEUE_EXPIRED"
    assert not instance.scheduler.active and not instance.scheduler.waiting


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["exec", "ssh_exec"])
@pytest.mark.parametrize("denial", ["SHELL_DISABLED", "TASKS_DISABLED"])
async def test_typed_remote_scheduler_does_not_bypass_local_permissions(agent, monkeypatch, tool, denial):
    instance, project, _ = agent
    if denial == "TASKS_DISABLED":
        project = {**project, "allow_tasks": False}

    async def unexpected(*args, **kwargs):
        pytest.fail("denied SSH must not start a process or connection")

    monkeypatch.setattr(instance, "run_process", unexpected)
    call = remote_call({**project, "id": "denied"}, tool, "host.example", "remote-denied")
    await instance.handle(call)
    assert instance.journal.status(call["id"])["result"]["error"]["code"] == denial
    assert not instance.scheduler.active and not instance.scheduler.waiting
