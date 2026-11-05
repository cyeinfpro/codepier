"""Strict compatibility, relaxed conflicts and bounded overtaking contracts."""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.resource_queue import Claim, ResourceQueue, canonical_path, claims_for
from agent.scheduler import ProjectScheduler
from shared.core_contracts import Exec, Read, Write, Edit
from shared.scheduler_config import validate_scheduler, effective_scheduler
from shared.util import DevError


def claim(name="/root", *, write=False, holder="file", stable=False):
    return Claim("agent", "path", name, write, holder, stable)


@pytest.mark.parametrize("left,right,expected", [
    (claim(), claim(write=True), True),
    (claim(write=True), claim(write=True), True),
    (claim(), claim(write=True, holder="process"), False),
    (claim(write=True), claim(holder="process"), False),
    (claim(write=True), claim(holder="process", stable=True), True),
    (claim(write=True), claim(write=True, holder="process"), True),
    (claim(holder="process"), claim(write=True, holder="process"), True),
    (claim(write=True, holder="process"), claim(write=True, holder="process"), True),
    (claim(), claim(holder="process"), False),
])
def test_relaxed_conflict_matrix_is_symmetric(left, right, expected):
    assert left.conflicts(right) is expected
    assert right.conflicts(left) is expected
    assert left.conflicts(right, relaxed=False) is (left.write or right.write)


def test_services_and_other_namespaces_do_not_relax():
    left = Claim("agent", "service", "db", False, "file")
    right = Claim("agent", "service", "db", True, "process")
    assert left.conflicts(right)
    assert not claim("/a").conflicts(claim("/b", write=True))
    assert not claim().conflicts(Claim("ssh:host:22", "path", "/root", True, "process"))


def enter(queue, identifier, claims, **kwargs):
    token = object()
    queue.enqueue(token, identifier, claims, **kwargs)
    if not queue.blocked(token):
        queue.grant(token)
    return token


def test_bypass_requires_long_running_root_and_preserves_writer_turn():
    clock = [0.]
    queue = ResourceQueue(semantics="relaxed", clock=lambda: clock[0])
    root = enter(queue, "root", [claim("/a", write=True, holder="process")])
    writer = enter(queue, "writer", [claim("/a", write=True, holder="process"),
                                      claim("/b", write=True, holder="process")])
    count = []
    reader = enter(queue, "reader", [claim("/b", write=True)], bounded=True, on_bypass=count.append)
    assert queue.blocked(reader)
    clock[0] = 6
    assert not queue.blocked(reader)
    queue.grant(reader)
    assert count == [1]
    queue.discard(root)
    assert queue.blocked(writer)
    assert queue.blockers(writer) == ["reader"]
    # The bypassed operation itself cannot become a new overtaking root.
    clock[0] = 20
    next_reader = enter(queue, "next", [claim("/b", write=True)], bounded=True)
    assert queue.blocked(next_reader)
    queue.discard(reader)
    assert not queue.blocked(writer)
    assert queue.blocked(next_reader)
    queue.grant(writer)
    assert queue.blocked(next_reader)
    queue.discard(writer)
    assert not queue.blocked(next_reader)


@pytest.mark.parametrize("semantics,bounded,strict", [
    ("strict", True, False), ("relaxed", False, False), ("relaxed", True, True),
])
def test_strict_order_or_unbounded_process_never_bypasses(semantics, bounded, strict):
    clock = [0.]
    queue = ResourceQueue(semantics=semantics, clock=lambda: clock[0])
    enter(queue, "root", [claim("/a", write=True, holder="process")])
    enter(queue, "writer", [claim("/", write=True)])
    clock[0] = 10
    reader = enter(queue, "reader", [claim("/b")], bounded=bounded, strict=strict)
    assert queue.blocked(reader)


def test_capacity_waiter_without_running_root_cannot_be_bypassed():
    queue = ResourceQueue(semantics="relaxed")
    writer = object()
    queue.enqueue(writer, "capacity-waiter", [claim(write=True)])
    reader = enter(queue, "read", [claim()], bounded=True)
    assert queue.blocked(reader)


def test_live_marks_both_arrival_orders_even_if_writer_finishes():
    queue = ResourceQueue(semantics="relaxed")
    reader = enter(queue, "reader", [claim()])
    writer = enter(queue, "writer", [claim(write=True, holder="process")])
    assert not queue.blocked(reader)
    assert queue.concurrency_for("reader") == {"mode": "live", "overlapping_writers": ["writer"]}
    queue.discard(writer)
    assert queue.concurrency_for("reader")["overlapping_writers"] == ["writer"]
    queue.discard(reader)
    assert queue.concurrency_for("reader") is None


@pytest.mark.asyncio
async def test_reconfigure_rebuilds_waiting_conflicts_without_cancelling_owners():
    queue = ResourceQueue(semantics="strict")
    owner = enter(queue, "process", [claim(write=True, holder="process")])
    reader = enter(queue, "reader", [claim()])
    assert queue.blocked(reader)
    queue.reconfigure("relaxed")
    assert not queue.blocked(reader)
    queue.grant(reader)
    queue.reconfigure("strict")
    new = enter(queue, "later", [claim()])
    assert queue.blocked(new)
    assert reader in queue.active and owner in queue.active


@pytest.mark.asyncio
async def test_joint_timer_grants_bypass_and_cancellation_cleans_every_edge():
    queue = ResourceQueue(semantics="relaxed", bypass_seconds=.02)
    scheduler = ProjectScheduler({"adaptive": False, "resource_semantics": "relaxed"}, resources=queue)
    got = asyncio.Event()
    async def writer():
        async with scheduler.slot("writer", "P", claims=[claim("/", write=True)]):
            pass
    async def reader():
        async with scheduler.slot("reader", "P", "read", claims=[claim("/b")]):
            got.set()
    async with queue.slot("long", [claim("/a", write=True, holder="process")], lambda _: None):
        w = asyncio.create_task(writer())
        await asyncio.sleep(0)
        r = asyncio.create_task(reader())
        await asyncio.wait_for(got.wait(), .5)
        await r
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
    assert not queue.active and not queue.waiting and not queue._entries
    assert not queue._dependents and not queue._dependencies and queue._timer is None


@pytest.mark.asyncio
async def test_fail_fast_has_no_side_effect_and_cleans_joint_ticket():
    queue = ResourceQueue()
    scheduler = ProjectScheduler({"adaptive": False}, resources=queue)
    executed = []
    async with queue.slot("busy", [claim(write=True, holder="process")], lambda _: None):
        with pytest.raises(DevError) as caught:
            async with scheduler.slot("write", "P", "read", claims=[claim(write=True)],
                                      wait_policy="fail_fast", initial_wait_seconds=.01):
                executed.append(True)
        assert caught.value.code == "RESOURCE_BUSY"
        assert caught.value.details["blocked_by"] == ["busy"]
        assert not executed and not scheduler.waiting and not scheduler.active
        assert not queue.waiting


@pytest.mark.parametrize("tool", ["searches_start", "code_symbols", "history_list"])
def test_scoped_reads_claim_requested_subtree(tmp_path, tool):
    engine = SimpleNamespace(path=lambda root, path, *args: root / path)
    result = claims_for(engine, tool, {}, {"path": "src/backend"}, tmp_path)
    assert result == [Claim("agent", "path", canonical_path(tmp_path / "src/backend"), False)]


@pytest.mark.parametrize("tool", ["project_checkpoint", "worktrees_create"])
def test_snapshot_publication_reads_source_stably(tmp_path, tool):
    result = claims_for(SimpleNamespace(config={}), tool, {}, {}, tmp_path)
    assert not result[0].write
    assert result[0].holder == "process" and result[0].stable
    if tool == "worktrees_create":
        assert len(result) >= 3 and all(claim.write for claim in result[1:])
    else:
        assert len(result) == 1


def test_public_queue_options_preserve_explicit_defaults_and_reject_unknowns():
    assert Read(project="P", path="a", options={"order": "strict"}).options == {"order": "strict"}
    assert Write(project="P", path="a", expected_sha256="new", idempotency_key="new-file-key",
                 options={"wait_policy": "fail_fast"}).options == {"wait_policy": "fail_fast"}
    assert Edit(project="P", path="a", expected_sha256="a"*64, idempotency_key="edit-file-key",
                edits=[{"old_text":"a","new_text":"b"}], options={"order":"strict"}).options["order"] == "strict"
    with pytest.raises(ValidationError):
        Read(project="P", path="a", options={"priority": 100})
    parsed = Exec(project="P", command="pytest", idempotency_key="exec-command",
                  resources=[{"name":"src", "mode":"read_stable"}], isolation="snapshot")
    assert parsed.resources[0].mode == "read_stable"
    with pytest.raises(ValidationError):
        Exec(project="P", command="pytest", idempotency_key="exec-command",
             isolation="snapshot", snapshot_artifacts=["../outside"])


def test_scheduler_semantics_defaults_strict_and_panel_can_select_relaxed():
    assert validate_scheduler({})["resource_semantics"] == "strict"
    assert effective_scheduler({}, {"resource_semantics": "relaxed"})["resource_semantics"] == "relaxed"
    with pytest.raises(ValueError):
        validate_scheduler({"resource_semantics": "unsafe"})


def test_maximum_sparse_dependency_chain_does_not_recurse():
    clock = [0.]
    queue = ResourceQueue(semantics="relaxed", clock=lambda: clock[0])
    root = enter(queue, "root", [claim("/node0", write=True, holder="process")])
    last = root
    for index in range(1000):
        last = object()
        queue.enqueue(last, "waiting-" + str(index), [
            claim("/node" + str(index), write=True, holder="process"),
            claim("/node" + str(index + 1), write=True, holder="process")])
    clock[0] = 10
    candidate = object()
    queue.enqueue(candidate, "short", [claim("/node1000", write=True)], bounded=True)
    assert queue._active_roots(last) == {root}
    assert not queue.blocked(candidate)


def test_batch_queue_options_keep_nested_file_defaults():
    parsed = Read(project="P", operation="batch", options={"items":[{"path":"a"}], "order":"strict"})
    assert parsed.options["items"] == [{"path":"a", "offset":1, "limit":2000, "expected_sha256":""}]
    assert parsed.options["order"] == "strict" and "wait_policy" not in parsed.options
