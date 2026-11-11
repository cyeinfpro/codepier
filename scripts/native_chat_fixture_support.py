"""Shared helpers for isolated native-chat platform acceptance."""

from contextlib import closing
import json
import time
import uuid
from shared.native_cli import database


def wait(predicate, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Native chat condition not observed before deadline")


def snapshot(manager, sid):
    with closing(database(manager.directory)) as db:
        row = dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
        raw = b"".join(
            bytes(r[0])
            for r in db.execute("SELECT data FROM output WHERE session=? ORDER BY offset", (sid,))
        )
        events = [json.loads(line) for line in raw.splitlines()]
        receipts = [
            dict(r)
            for r in db.execute(
                "SELECT id,kind,state FROM commands WHERE session=? ORDER BY rowid", (sid,)
            )
        ]
    return row, events, receipts


def receipt_completed(manager, sid, receipt):
    row, events, receipts = snapshot(manager, sid)
    if row["status"] not in ("starting", "running"):
        raise AssertionError("Worker stopped: " + row["error"])
    return any(r["id"] == receipt and r["state"] == "completed" for r in receipts)


def stop_child(manager, project_row, sid, child):
    if child.poll() is None:
        manager.action("stop", project_row, {"id": sid, "receipt": uuid.uuid4().hex})
        child.wait(timeout=10)
