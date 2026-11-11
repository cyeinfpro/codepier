"""Durable routing for native room inputs awaiting a recipient."""

from __future__ import annotations

import json

from hub.collaboration.common import canonical, digest
from shared.util import DevError


BATCH_SIZE = 64
BLOCKED_MEMBERS = {"paused", "removed", "legacy"}


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_waiting_inputs(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT NOT NULL UNIQUE REFERENCES collaboration_messages(id) ON DELETE CASCADE,
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),
        message_sequence INTEGER NOT NULL, source_version INTEGER NOT NULL,
        source_digest TEXT NOT NULL, member_id TEXT NOT NULL, follow_lead INTEGER NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_waiting_inputs_conversation
        ON room_waiting_inputs(conversation_id,message_sequence,sequence)""")
    # Old panel inputs with an explicit or implicit saved member cannot be
    # distinguished. Preserve their assignment and resume their original lane.
    # Existing mailbox inputs already have a durable recipient.
    if not db.execute("SELECT 1 FROM meta WHERE key='room_waiting_inputs_backfill_v1'").fetchone():
        rows = db.execute("""SELECT m.id,m.conversation_id,m.conversation_sequence,m.version,m.body
            FROM collaboration_messages m WHERE m.origin='panel_owner' AND m.state='saved'
            AND json_valid(m.body) AND json_extract(m.body,'$.room_routing.state')='waiting'
            AND NOT EXISTS (SELECT 1 FROM dot_mailbox_entries e
                WHERE e.kind='message' AND e.object_id=m.id)
            ORDER BY m.conversation_id,m.conversation_sequence""")
        for message_id, conversation_id, sequence, version, data in rows:
            body = json.loads(data)
            member_id = body.get("room_member_id", "")
            db.execute(
                """INSERT OR IGNORE INTO room_waiting_inputs
                (message_id,conversation_id,message_sequence,source_version,source_digest,member_id,follow_lead)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    message_id,
                    conversation_id,
                    sequence,
                    version,
                    digest(body.get("body_text", "")),
                    member_id,
                    int(not member_id and not body.get("work_id")),
                ),
            )
        db.execute("INSERT INTO meta VALUES ('room_waiting_inputs_backfill_v1','1')")


def route_input(c, room, member):
    member_id = member["id"] if member else ""
    if not member:
        reason = "lead_member_required"
    elif member["status"] in BLOCKED_MEMBERS:
        reason = "member_" + member["status"]
    elif room["state"] != "active":
        reason = "room_paused"
    elif c.rooms.state(member) not in {"ready_chat", "ready_work"}:
        reason = "member_not_connected"
    else:
        reason = ""
    return {
        "state": "waiting" if reason else "queued",
        "member_id": member_id,
        "reason_code": reason,
    }


def record_input(c, message, member, owner, *, follow_lead=False, allow_dispatch=True):
    """Called in the caller's input transaction after all source proofs are saved."""
    if not allow_dispatch:
        return
    body = json.loads(message["body"])
    if member and member["status"] not in BLOCKED_MEMBERS:
        room = c.store.one(
            "SELECT * FROM conversation_rooms WHERE id=?", (message["conversation_id"],)
        )
        if not _earlier_input(c, room, message["conversation_sequence"], member):
            c.room_consumers.enqueue(message, member, owner)
            return
        route = {
            "state": "waiting",
            "member_id": member["id"],
            "reason_code": "waiting_earlier_input",
        }
        _save_route(c, message, body, route, notify=False)
    c.store.execute(
        """INSERT OR IGNORE INTO room_waiting_inputs
        (message_id,conversation_id,message_sequence,source_version,source_digest,member_id,follow_lead)
        VALUES (?,?,?,?,?,?,?)""",
        (
            message["id"],
            message["conversation_id"],
            message["conversation_sequence"],
            message["version"],
            digest(body.get("body_text", "")),
            member["id"] if member else "",
            int(follow_lead),
        ),
    )


def _earlier_input(c, room, sequence, member):
    lead = c.room_runtime.recipient(room, "")
    custom = c.room_permissions.channel_mode(room["id"], "weixin") == "custom"
    return c.store.one(
        """SELECT 1 FROM room_waiting_inputs w
        JOIN collaboration_messages m ON m.id=w.message_id
        LEFT JOIN weixin_bindings b ON b.id=json_extract(m.body,'$.external_origin.binding_id')
        WHERE w.conversation_id=? AND w.message_sequence<?
        AND (m.origin!='external_channel' OR (
            b.state='active' AND b.generation=json_extract(m.body,'$.external_origin.generation')))
        AND CASE WHEN m.origin='external_channel' AND ?
                 THEN b.native_member_id=?
                 ELSE (w.follow_lead=1 AND ?) OR (w.follow_lead=0 AND w.member_id=?) END
        LIMIT 1""",
        (
            room["id"],
            sequence,
            custom,
            member["id"],
            bool(lead and lead["id"] == member["id"]),
            member["id"],
        ),
    )


def _save_route(c, message, body, route, *, notify=True):
    if body.get("room_routing") != route or body.get("room_member_id") != route["member_id"]:
        body["room_routing"] = route
        body["room_member_id"] = route["member_id"]
        c.store.execute(
            "UPDATE collaboration_messages SET body=? WHERE id=?", (canonical(body), message["id"])
        )
        if notify:
            c.rooms.change(message["conversation_id"], "message", message["id"], message["version"])


def _finish(c, row, message, body, reason):
    _save_route(
        c,
        message,
        body,
        {"state": "terminal", "member_id": row["member_id"], "reason_code": reason},
    )
    c.store.execute("DELETE FROM room_waiting_inputs WHERE sequence=?", (row["sequence"],))


def _dispatch(c, row):
    message = c.store.one("SELECT * FROM collaboration_messages WHERE id=?", (row["message_id"],))
    room = c.store.one("SELECT * FROM conversation_rooms WHERE id=?", (row["conversation_id"],))
    if not message or not room:
        c.store.execute("DELETE FROM room_waiting_inputs WHERE sequence=?", (row["sequence"],))
        return
    body = json.loads(message["body"])
    if (
        message["state"] != "saved"
        or message["version"] != row["source_version"]
        or digest(body.get("body_text", "")) != row["source_digest"]
    ):
        _finish(c, row, message, body, "source_unavailable")
        return
    if room["state"] == "paused":
        return
    if room["state"] != "active" or not room["room_protocol"]:
        _finish(c, row, message, body, "source_unavailable")
        return
    owner = None
    follow_lead = bool(row["follow_lead"])
    member_id = row["member_id"]
    if message["origin"] == "external_channel":
        try:
            native = c.room_channels.native
            binding, _, owner, _, _ = native.source_binding(message)
            if follow_lead and not native.follows_room(binding):
                follow_lead = False
                member_id = binding["native_member_id"]
        except DevError as exc:
            if exc.code in {
                "WEIXIN_BINDING_INACTIVE",
                "WEIXIN_CREDENTIAL_CHANGED",
                "WEIXIN_DELIVERY_SOURCE_CHANGED",
                "WEIXIN_DELIVERY_SCOPE_CHANGED",
                "WEIXIN_ACCOUNT_NOT_FOUND",
            }:
                _finish(c, row, message, body, "source_unavailable")
            return
    elif message["origin"] != "panel_owner" or message["author"] != room["owner_user_id"]:
        _finish(c, row, message, body, "source_unavailable")
        return
    if (work_id := body.get("work_id")) and body.get("work_intent_change", True):
        try:
            goal = c.room_runtime.goal(work_id)
        except DevError:
            goal = None
        if (
            not goal
            or goal["conversation_id"] != room["id"]
            or goal["state"] in {"cancelled", "expired", "completed"}
        ):
            _finish(c, row, message, body, "work_unavailable")
            return
        intent = c.store.one(
            "SELECT source_message_id FROM room_work_intents WHERE goal_id=?", (goal["id"],)
        )
        if intent and intent["source_message_id"] != message["id"]:
            _finish(c, row, message, body, "source_unavailable")
            return
    member = (
        c.room_runtime.recipient(room, "")
        if follow_lead
        else c.store.one(
            "SELECT * FROM room_members WHERE id=? AND conversation_id=?", (member_id, room["id"])
        )
    )
    if not follow_lead and (not member or member["status"] == "removed"):
        _finish(c, row, message, body, "member_removed")
        return
    if member and member["status"] == "removed":
        member = None
    ready = member and member["status"] not in BLOCKED_MEMBERS
    if ready and _earlier_input(c, room, row["message_sequence"], member):
        return
    _save_route(c, message, body, route_input(c, room, member))
    if ready:
        current = c.store.one("SELECT * FROM collaboration_messages WHERE id=?", (message["id"],))
        c.room_consumers.enqueue(current, member, owner)
        c.store.execute("DELETE FROM room_waiting_inputs WHERE sequence=?", (row["sequence"],))


def retire_input(c, message_id, successor_id):
    """Remove superseded source work from waiting and mailbox journals."""
    if not message_id:
        return
    message = c.store.one("SELECT * FROM collaboration_messages WHERE id=?", (message_id,))
    waiting = c.store.one("SELECT 1 FROM room_waiting_inputs WHERE message_id=?", (message_id,))
    pending = c.store.one(
        """SELECT 1 FROM dot_mailbox_entries
        WHERE kind='message' AND object_id=? AND acknowledged_at IS NULL AND superseded_by='' LIMIT 1""",
        (message_id,),
    )
    if message and (waiting or pending):
        body = json.loads(message["body"])
        _save_route(
            c,
            message,
            body,
            {
                "state": "terminal",
                "member_id": body.get("room_member_id", ""),
                "reason_code": "source_unavailable",
            },
        )
    c.store.execute("DELETE FROM room_waiting_inputs WHERE message_id=?", (message_id,))
    c.store.execute(
        """UPDATE dot_mailbox_entries SET superseded_by=?
        WHERE kind='message' AND object_id=? AND acknowledged_at IS NULL AND superseded_by='' """,
        ("source:" + successor_id, message_id),
    )


def recover_waiting(c, *, conversation_id=None):
    """One bounded scan; periodic scans persist their progress past blocked inputs."""
    with c.store.transaction():
        if conversation_id:
            rows = c.store.all(
                """SELECT * FROM room_waiting_inputs WHERE conversation_id=?
                ORDER BY message_sequence,sequence LIMIT ?""",
                (conversation_id, BATCH_SIZE),
            )
        else:
            cursor = c.store.one("SELECT value FROM meta WHERE key='room_waiting_inputs_cursor'")
            after = int(cursor["value"]) if cursor else 0
            rows = c.store.all(
                "SELECT * FROM room_waiting_inputs WHERE sequence>? ORDER BY sequence LIMIT ?",
                (after, BATCH_SIZE),
            )
            next_cursor = rows[-1]["sequence"] if len(rows) == BATCH_SIZE else 0
            c.store.execute(
                "INSERT OR REPLACE INTO meta VALUES ('room_waiting_inputs_cursor',?)",
                (str(next_cursor),),
            )
        for row in rows:
            try:
                with c.store.savepoint():
                    _dispatch(c, row)
            except DevError:
                # Preserve the original input for a later scan and keep
                # independent waiting inputs recoverable in this pass.
                continue


def fence_follow_poll(c, conversation_id):
    """Route changes fence live polls without changing source identity or cursors."""
    c.store.execute(
        """UPDATE weixin_accounts SET poll_until=0,poll_fence=poll_fence+1,next_poll=0
        WHERE id IN (SELECT account_id FROM weixin_bindings WHERE conversation_id=?
            AND routing_mode='follow_room' AND state IN ('active','pairing','awaiting_owner'))""",
        (conversation_id,),
    )
