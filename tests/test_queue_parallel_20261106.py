"""Regressions for docs/QUEUE_DIAGNOSIS_2026-11-06.md.

1. Rejected native history must not be resent in a hot loop (Hub status +
   Agent pause/backoff, also against a legacy Hub that only omits offsets).
2. Control receipts are not serialized behind transcript backfill
   (Agent send priority, Hub per-connection sync lane).
3. Strict resource semantics let short reads pass a queued command that is
   stuck behind a long-running root, without overtaking queued edits.
"""
import asyncio
import base64
from contextlib import closing
import time
from types import SimpleNamespace
import uuid

import pytest

from agent.native_sync_flow import SyncFlow, WINDOW, fingerprint
from agent.resource_queue import Claim, ResourceQueue
from agent.send_priority import PrioritySendLock, is_bulk
from shared.native_cli import database, public
from tests.test_chat_sse import service  # noqa: F401  (fixture)
from tests.test_native_cli import native  # noqa: F401  (fixture)


# ---------------------------------------------------------------- send gate
@pytest.mark.asyncio
async def test_control_frames_jump_queued_bulk_frames_fifo_within_class():
    lock, order = PrioritySendLock(), []
    await lock.acquire(True)  # a bulk frame is on the wire

    async def sender(name, bulk):
        async with lock.hold(bulk):
            order.append(name)

    tasks = [asyncio.create_task(sender("bulk1", True)), asyncio.create_task(sender("bulk2", True))]
    await asyncio.sleep(0)
    tasks += [asyncio.create_task(sender("accepted", False)), asyncio.create_task(sender("result", False))]
    await asyncio.sleep(0)
    lock.release()
    await asyncio.gather(*tasks)
    assert order == ["accepted", "result", "bulk1", "bulk2"]
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancelled_waiter_hands_lock_on():
    lock = PrioritySendLock()
    await lock.acquire()
    waiter = asyncio.create_task(lock.acquire(True))
    other = asyncio.create_task(lock.acquire(True))
    await asyncio.sleep(0)
    waiter.cancel()
    lock.release()
    await asyncio.wait_for(other, 1)
    assert lock.locked()
    lock.release()
    assert not lock.locked()


def test_only_transcript_and_artifact_frames_are_bulk():
    assert is_bulk({"type": "native_sync"}) and is_bulk({"type": "artifact_chunk"})
    for kind in ("accepted", "started", "result", "status", "trace", "heartbeat", "native_reply", "output"):
        assert not is_bulk({"type": kind})


# ---------------------------------------------------------------- sync flow
def chunk(sid, offset, size):
    return {"id": sid, "offset": offset, "data": base64.b64encode(b"x" * size).decode()}


def make_flow(tmp_path=None):
    now = [1000.0]
    flow = SyncFlow(tmp_path / "pause.json" if tmp_path else None, clock=lambda: now[0], monotonic=lambda: now[0])
    return flow, now


def test_window_bounds_unacknowledged_transcript_packets():
    flow, _ = make_flow()
    for _ in range(WINDOW):
        assert flow.window_open()
        flow.sent(flow.next_seq(), [chunk("a", 0, 10)])
    assert not flow.window_open()
    flow.sent(flow.next_seq(), [])  # metadata-only packets do not consume the window
    offsets = {}
    flow.on_ack({"offsets": {"a": 10}, "seqs": [1]}, offsets)
    assert flow.window_open() and offsets == {"a": 10}


def test_window_reopens_after_ack_timeout_and_backs_off_session():
    flow, now = make_flow()
    flow.sent(flow.next_seq(), [chunk("a", 0, 10)])
    flow.sent(flow.next_seq(), [chunk("b", 0, 10)])
    now[0] += 60
    assert flow.window_open()
    assert {"a", "b"} <= flow.skip()


def test_legacy_hub_missing_offset_stops_hot_resend_with_bounded_backoff():
    """Old Hub: rejected session simply has no offset in the FIFO-matched ACK."""
    flow, now = make_flow()
    offsets, delays = {}, []
    for _ in range(12):
        flow.sent(flow.next_seq(), [chunk("old", 0, 65536)])
        flow.on_ack({"offsets": {}}, offsets)  # no seqs: legacy
        assert "old" in flow.skip()
        until, delay = flow.backoff["old"]
        delays.append(delay)
        now[0] = until + .001
        assert "old" not in flow.skip()
    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
    assert max(delays) == 300.0
    assert "old" not in offsets


def test_progress_clears_backoff_and_quota_defers_longer():
    flow, _ = make_flow()
    offsets = {}
    flow.sent(flow.next_seq(), [chunk("a", 0, 10)])
    flow.on_ack({"offsets": {"a": 0}, "seqs": [1], "deferred": {"a": "cache_quota"}}, offsets)
    assert flow.backoff["a"][1] == 30.0
    flow.sent(flow.next_seq(), [chunk("a", 0, 10)])
    flow.on_ack({"offsets": {"a": 10}, "seqs": [2]}, offsets)
    assert "a" not in flow.backoff and offsets["a"] == 10


def test_rejected_session_pause_persists_across_restart_and_probes(tmp_path):
    flow, now = make_flow(tmp_path)
    row = {"id": "s1", "project_id": "p", "root": "/old", "device_id": "d"}
    flow.observe_rows([row])
    flow.sent(flow.next_seq(), [chunk("s1", 0, 100)])
    flow.on_ack({"offsets": {}, "seqs": [1], "rejected": {"s1": "mapping_changed"}}, {})
    assert "s1" in flow.skip() and "s1" not in flow.backoff
    flow.write(flow.dump())

    # A reconnect or Agent restart keeps the refusal: no hot loop resumes.
    again, _ = make_flow(tmp_path)
    again.reset_connection()
    assert "s1" in again.skip()
    assert again.paused["s1"]["reason"] == "mapping_changed"
    assert again.due_probes() == []
    now_again = again.paused["s1"]["until"] + 1
    again.clock = lambda: now_again
    assert again.due_probes() == ["s1"]   # metadata-only re-offer
    assert again.due_probes() == []       # one probe per interval
    # Hub accepts the session again (mapping restored): chunks resume.
    again.on_ack({"offsets": {"s1": 0}}, {})
    assert "s1" not in again.skip()


def test_repeat_rejections_grow_probe_interval_and_session_mapping_change_unpauses():
    flow, now = make_flow()
    row = {"id": "s1", "project_id": "p", "root": "/old", "device_id": "d"}
    flow.observe_rows([row])
    intervals = []
    for _ in range(8):
        flow.on_ack({"offsets": {}, "rejected": {"s1": "mapping_changed"}}, {})
        intervals.append(flow.paused["s1"]["until"] - now[0])
    assert intervals[0] == 120 and intervals[-1] == 3600 and intervals == sorted(intervals)
    assert flow.paused["s1"]["fp"] == fingerprint(row)
    flow.observe_rows([dict(row, root="/new")])
    assert "s1" not in flow.paused


def test_corrupt_pause_file_is_ignored(tmp_path):
    (tmp_path / "pause.json").write_text("{not json")
    flow, _ = make_flow(tmp_path)
    assert flow.paused == {}


def test_agent_snapshot_skips_paused_sessions_and_closed_window(native):  # noqa: F811
    obj, project = native
    ids = []
    with closing(database(obj.directory)) as db, db:
        for i in range(2):
            sid = uuid.uuid4().hex
            ids.append(sid)
            db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,size) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (sid, project['id'], project['device_id'], project['root'], project['root'], 'codex', 's', 'exited', time.time() + i, time.time(), 1))
            db.execute('INSERT INTO output VALUES (?,?,?)', (sid, 0, b'a'))
    packet = obj.snapshot(frozenset({ids[0]}), True)
    assert {c['id'] for c in packet['chunks']} == {ids[1]}
    assert {r['id'] for r in packet['sessions']} >= set(ids)  # metadata still offered
    assert obj.snapshot(frozenset(), False)['chunks'] == []


# ---------------------------------------------------------------- Hub side
def row_of(obj, sid):
    with closing(database(obj.directory)) as db:
        return public(db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())


@pytest.mark.asyncio
async def test_hub_reports_rejected_mapping_and_withholds_offset(service):  # noqa: F811
    obj, project, sid = service
    sent = []

    async def send(data): sent.append(data)
    moved = dict(row_of(obj, sid), root='/somewhere/else')
    await obj.receive('device', SimpleNamespace(send=send),
                      {'type': 'native_sync', 'seq': 7, 'sessions': [moved], 'chunks': [chunk(sid, 0, 8)]})
    ack = sent[-1]
    assert ack['seqs'] == [7]
    assert sid not in ack['offsets']
    assert ack['rejected'] == {sid: 'mapping_changed'}
    # Nothing was adopted or re-homed.
    assert row_of(obj, sid)['root'] == project['root']


@pytest.mark.asyncio
async def test_hub_legacy_packet_ack_shape_is_unchanged(service):  # noqa: F811
    obj, _, sid = service
    sent = []

    async def send(data): sent.append(data)
    await obj.receive('device', SimpleNamespace(send=send),
                      {'type': 'native_sync', 'sessions': [row_of(obj, sid)], 'chunks': [chunk(sid, 0, 8)]})
    assert sent[-1] == {'type': 'native_ack', 'offsets': {sid: 8}}
    assert obj._persist_sync('device', {'sessions': [], 'chunks': []}) == ({}, False)


@pytest.mark.asyncio
async def test_hub_submit_never_blocks_reader_and_keeps_wire_order(service):  # noqa: F811
    obj, _, sid = service
    gate, sent = asyncio.Event(), []

    async def send(data):
        await gate.wait()   # a slow ACK path must not stall the frame reader
        sent.append(data)
    connection = SimpleNamespace(send=send)
    row = row_of(obj, sid)
    for seq in range(1, 4):
        # submit() is synchronous: the reader returns immediately each time.
        obj.submit('device', connection, {'type': 'native_sync', 'seq': seq, 'sessions': [row],
                                          'chunks': [chunk(sid, (seq - 1) * 8, 8)]})
    gate.set()
    await asyncio.wait_for(connection._native_sync_lane.task, 5)
    assert [a['seqs'] for a in sent] == [[1], [2], [3]]
    assert [a['offsets'][sid] for a in sent] == [8, 16, 24]


@pytest.mark.asyncio
async def test_hub_lane_coalesces_without_losing_rows_or_acking_unstored_bytes(service):  # noqa: F811
    obj, _, sid = service
    gate, sent = asyncio.Event(), []

    async def send(data):
        await gate.wait()
        sent.append(data)
    connection = SimpleNamespace(send=send)
    row = row_of(obj, sid)
    for seq in range(1, 10):
        obj.submit('device', connection, {'type': 'native_sync', 'seq': seq, 'sessions': [row],
                                          'chunks': [chunk(sid, 0, 8)]})
    assert len(connection._native_sync_lane.queue) <= 4
    gate.set()
    await asyncio.wait_for(connection._native_sync_lane.task, 5)
    acked = [seq for ack in sent for seq in ack['seqs']]
    assert sorted(acked) == list(range(1, 10))   # every packet is answered once
    assert max(ack['offsets'][sid] for ack in sent) == 8  # duplicates were not stored twice


@pytest.mark.asyncio
async def test_hub_db_failure_acks_with_no_offsets_instead_of_dropping_socket(service, monkeypatch):  # noqa: F811
    obj, _, sid = service
    sent = []

    async def send(data): sent.append(data)

    def boom(*args): raise RuntimeError('disk')
    monkeypatch.setattr(obj, '_persist_sync_detailed', boom)
    await obj.receive('device', SimpleNamespace(send=send),
                      {'type': 'native_sync', 'seq': 3, 'sessions': [row_of(obj, sid)], 'chunks': []})
    assert sent == [{'type': 'native_ack', 'offsets': {}, 'seqs': [3]}]
    assert obj.sync_errors == 1


# ---------------------------------------------------------------- resources
def claim(name, *, write=False, holder="file"):
    return Claim("agent", "path", name, write, holder)


def enter(queue, identifier, claims, **kwargs):
    token = object()
    queue.enqueue(token, identifier, claims, **kwargs)
    if not queue.blocked(token):
        queue.grant(token)
    return token


def convoy(blocker_claims, reader_claims, *, bounded=True, strict=False):
    clock = [0.]
    queue = ResourceQueue(semantics="strict", clock=lambda: clock[0])
    enter(queue, "git", [claim("/repo/.git", write=True, holder="process")])
    waiting = enter(queue, "exec", blocker_claims)
    reader = enter(queue, "read", reader_claims, bounded=bounded, strict=strict)
    return queue, clock, waiting, reader


def test_strict_read_passes_queued_command_stuck_behind_long_root():
    queue, clock, waiting, reader = convoy(
        [claim("/repo/.git", write=True, holder="process"), claim("/repo", write=True, holder="process")],
        [claim("/repo/src/app.py")])
    assert queue.blocked(reader)          # root has not run long yet
    clock[0] = 6
    assert not queue.blocked(reader)      # convoy relief
    queue.grant(reader)
    assert queue.blocked(waiting)         # the command still waits for both
    assert queue.blockers(waiting) == ["git", "read"]


@pytest.mark.parametrize("blocker,reader,bounded,strict", [
    # A queued *edit* keeps read-your-writes.
    ([claim("/repo/.git", write=True, holder="process"), claim("/repo/src", write=True)], [claim("/repo/src/app.py")], True, False),
    # Writers never overtake in strict mode.
    ([claim("/repo/.git", write=True, holder="process"), claim("/repo", write=True, holder="process")], [claim("/repo/src/app.py", write=True)], True, False),
    # Explicit order=strict and unbounded work keep FIFO.
    ([claim("/repo/.git", write=True, holder="process"), claim("/repo", write=True, holder="process")], [claim("/repo/src/app.py")], True, True),
    ([claim("/repo/.git", write=True, holder="process"), claim("/repo", write=True, holder="process")], [claim("/repo/src/app.py")], False, False),
])
def test_strict_convoy_relief_never_reorders_edits_or_writers(blocker, reader, bounded, strict):
    queue, clock, _, token = convoy(blocker, reader, bounded=bounded, strict=strict)
    clock[0] = 60
    assert queue.blocked(token)


def test_strict_read_never_overlaps_running_root():
    queue, clock, _, reader = convoy(
        [claim("/repo", write=True, holder="process")], [claim("/repo/.git/HEAD")])
    clock[0] = 60
    assert queue.blocked(reader)


@pytest.mark.asyncio
async def test_strict_timer_wakes_waiting_reader():
    queue = ResourceQueue(semantics="strict", bypass_seconds=.02)
    root = await _hold(queue, "root", [claim("/a", write=True, holder="process")])
    exec_task = asyncio.create_task(_hold(queue, "exec", [claim("/a", write=True, holder="process"),
                                                         claim("/b", write=True, holder="process")]))
    await asyncio.sleep(0)
    granted = asyncio.Event()

    async def reader():
        async with queue.slot("read", [claim("/b/file")], lambda _: None, bounded=True):
            granted.set()
    task = asyncio.create_task(reader())
    await asyncio.wait_for(granted.wait(), 2)
    await task
    await queue.release(root)
    await queue.release(await exec_task)


async def _hold(queue, identifier, claims):
    token, event = object(), asyncio.Event()
    async with queue.condition:
        queue.enqueue(token, identifier, claims)
        queue._events[token] = event
        queue._callbacks[token] = lambda _: None
        queue.drain()
    await event.wait()
    return token


# ------------------------------------------- small-node memory (Seoul 2026-11-07)
MIB = 1024 ** 2
SEOUL_TOTAL = 952 * MIB


def test_seoul_sample_is_below_recovery_line_and_healthy_one_is_not():
    from agent.memory_relief import below_recovery
    assert below_recovery(SimpleNamespace(memory_available=76 * MIB, memory_total=SEOUL_TOTAL))
    assert not below_recovery(SimpleNamespace(memory_available=300 * MIB, memory_total=SEOUL_TOTAL))
    assert not below_recovery(SimpleNamespace(memory_available=None, memory_total=SEOUL_TOTAL))


def test_memory_relief_trims_under_pressure_rate_limited_and_rarely_when_idle():
    from agent.memory_relief import MemoryRelief, PRESSURE_INTERVAL, IDLE_INTERVAL
    now, calls = [0.0], []
    relief = MemoryRelief(clock=lambda: now[0], trimmer=lambda: calls.append(now[0]) or 0)
    tight = SimpleNamespace(memory_available=76 * MIB, memory_total=SEOUL_TOTAL)
    roomy = SimpleNamespace(memory_available=600 * MIB, memory_total=SEOUL_TOTAL)
    relief.observe(tight, idle=False)
    now[0] = PRESSURE_INTERVAL / 2
    relief.observe(tight, idle=False)
    now[0] = PRESSURE_INTERVAL + 1
    relief.observe(tight, idle=False)
    assert calls == [0.0, PRESSURE_INTERVAL + 1]
    now[0] += 60
    relief.observe(roomy, idle=False)          # busy and healthy: leave the heap alone
    relief.observe(roomy, idle=True)           # idle, but trimmed recently
    now[0] += IDLE_INTERVAL
    relief.observe(roomy, idle=True)
    assert len(calls) == 3


def test_memory_relief_primitives_never_raise():
    from agent import memory_relief
    assert isinstance(memory_relief.configure(), bool)
    freed = memory_relief.trim()
    assert freed is None or freed >= 0


def admission_counts(**overrides):
    counts = dict(global_count=0, global_bytes=0, node_count=0, project_count=0, node_bytes=0,
                  project_bytes=0, node_reads=0, project_reads=0, controls=0)
    counts.update(overrides)
    return counts


def test_memory_paused_node_still_admits_small_file_writes_but_not_commands():
    from hub.admission_policy import Budget, check_admission, LIGHT_MUTATION_BYTES, LIGHT_MUTATION_SLOTS
    from shared.util import DevError
    paused = Budget(read_reserve=8, reason="pressure", paused=True)
    for tool in ("write", "edit"):
        check_admission(paused, admission_counts(), tool, 4096)
    check_admission(paused, admission_counts(), "read", 100)
    for tool, size, counts in (("exec", 100, admission_counts()),
                               ("write", LIGHT_MUTATION_BYTES + 1, admission_counts()),
                               ("write", 100, admission_counts(node_count=LIGHT_MUTATION_SLOTS))):
        with pytest.raises(DevError) as caught:
            check_admission(paused, counts, tool, size)
        assert caught.value.code == "DEVICE_BUSY"
