"""Every Agent output packet satisfies Hub limits and histories make progress."""
from contextlib import closing
import time
import uuid
from shared.native_cli import database
from tests.test_native_cli import native


def test_omitted_sequence_releases_slot_without_advancing_cursor_or_delaying_retry():
    from agent.native_sync_flow import SyncFlow
    flow = SyncFlow()
    flow.inflight.extend([{"seq": 4, "chunks": {"dropped": 5}}, {"seq": 5, "chunks": {"persisted": 8}}])
    offsets = {"dropped": 1}
    flow.on_ack({"seqs": [4, 5], "coalesced_seqs": [4], "offsets": {"persisted": 8}}, offsets)
    assert offsets == {"dropped": 1, "persisted": 8}
    assert not flow.inflight and not flow.backoff


def test_many_small_history_chunks_stay_under_hub_packet_limit(native):
    obj,project=native
    ids=[]
    with closing(database(obj.directory)) as db,db:
        for i in range(45):
            sid=uuid.uuid4().hex;ids.append(sid)
            db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,size) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (sid,project['id'],project['device_id'],project['root'],project['root'],'codex','session '+str(i),'exited',time.time()+i,time.time(),2))
            db.execute('INSERT INTO output VALUES (?,?,?)',(sid,0,b'a'))
            db.execute('INSERT INTO output VALUES (?,?,?)',(sid,1,b'b'))
    seen=set()
    for _ in range(12):
        packet=obj.snapshot()
        assert len(packet['chunks'])<=16
        assert len(packet['sessions'])<=250
        for part in packet['chunks']:
            seen.add(part['id']);obj.offsets[part['id']]=part['offset']+1
    assert seen==set(ids)
    assert all(obj.offsets[sid]==2 for sid in ids)


def test_sync_metadata_joins_owner_for_bounded_page(native, monkeypatch):
    obj, project = native
    with closing(database(obj.directory)) as db, db:
        records = [(f"{i:032x}", project["id"], project["device_id"], project["root"],
                    project["root"], "codex", "fixture", "exited", i, i)
                   for i in range(1, 2001)]
        db.executemany(
            "INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", records,
        )
        db.executemany(
            "INSERT INTO native_security VALUES('session',?,'fixture-owner','fixture-space')",
            [(row[0],) for row in records],
        )
    old_connect = obj.connect_db
    trace = []
    from contextlib import contextmanager

    @contextmanager
    def traced_connection():
        with old_connect() as db:
            db.set_trace_callback(trace.append)
            yield db

    monkeypatch.setattr(obj, "connect_db", traced_connection)
    page1 = obj.snapshot(allow_chunks=False)["sessions"]
    page2 = obj.snapshot(allow_chunks=False)["sessions"]
    assert len(page1) == len(page2) == 250
    assert page1[0]["id"] != page2[0]["id"]
    assert all(row["_native_owner"] == "fixture-owner" and row["_native_space"] == "fixture-space"
               for row in page1 + page2)
    assert sum(line.strip().upper().startswith("SELECT") for line in trace) <= 10
