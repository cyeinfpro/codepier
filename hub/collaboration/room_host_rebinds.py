"""Owner-selected batch replacement using the existing candidate bindings."""

import uuid

from hub.principal import refresh_principal
from hub.collaboration.common import read_cursor, sign_cursor, validate
from shared import room_contracts as contracts
from shared.util import DevError


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_host_rebind_batches (
        id TEXT PRIMARY KEY, space_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        host_connection_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS room_host_rebind_items (
        batch_id TEXT NOT NULL REFERENCES room_host_rebind_batches(id),
        position INTEGER NOT NULL, member_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL DEFAULT '', flow_id TEXT NOT NULL DEFAULT '',
        candidate_id TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (batch_id, position))""")


class RoomHostRebinds:
    def __init__(self, rooms):
        self.rooms, self.c, self.store = rooms, rooms.c, rooms.store

    def owner(self, principal):
        self.c.guard()
        principal = refresh_principal(self.store, principal)
        self.c.owner(principal)
        return principal

    def create(self, raw, principal):
        args = validate(contracts.HostRebindBatch, raw)
        with self.store.transaction():
            principal = self.owner(principal)

            def perform():
                if not self.rooms.host_connections_enabled():
                    raise DevError(
                        "ROOM_CONNECTION_FEATURE_DISABLED", "独立连接接入当前未开启", 409
                    )
                host_id = args.get("host_connection_id") or ""
                host = self.rooms.select_host(principal, host_id) if host_id else None
                batch_id = uuid.uuid4().hex
                self.store.execute(
                    "INSERT INTO room_host_rebind_batches VALUES (?,?,?,?,?)",
                    (batch_id, principal.space_id, principal.user_id, host_id, self.c.clock()),
                )
                for position, request in enumerate(args["members"]):
                    member_id = request["member_id"]
                    conversation_id = flow_id = candidate_id = error = ""
                    try:
                        with self.store.savepoint():
                            member = self.rooms.member(member_id)
                            _, room = self.rooms.owner(principal, member["conversation_id"])
                            conversation_id = room["id"]
                            self.rooms.require_native(room)
                            if (
                                room["state"] != "active"
                                or member["status"] == "legacy"
                                or not self.rooms.can_prepare_replacement(member)
                            ):
                                raise DevError(
                                    "ROOM_MEMBER_INACTIVE", "此房间成员当前无法更换连接", 409
                                )
                            if member["binding_generation"] != request["expected_generation"]:
                                raise DevError("ROOM_BINDING_CHANGED", "成员已更换连接", 409)
                            if host_id and member["host_connection_id"] == host_id:
                                raise DevError(
                                    "ROOM_CONNECTION_ALREADY_PRESENT", "该成员已使用所选连接", 409
                                )
                            if self.rooms.pending.current(member_id):
                                raise DevError(
                                    "ROOM_BINDING_PENDING", "该成员已有待验证的连接", 409
                                )
                            flow = self.store.one(
                                "SELECT id FROM room_connect_flows WHERE member_id=?", (member_id,)
                            )
                            if not flow:
                                raise DevError("CONNECT_FLOW_NOT_FOUND", "没有这个接入流程", 404)
                            candidate = self.rooms.pending.prepare(member, principal, host=host)
                            candidate_id, flow_id = candidate["id"], flow["id"]
                            self.rooms.change(
                                conversation_id, "member", member_id, member["version"] + 1
                            )
                    except DevError as exc:
                        error = exc.code
                    self.store.execute(
                        "INSERT INTO room_host_rebind_items VALUES (?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            position,
                            member_id,
                            conversation_id,
                            flow_id,
                            candidate_id,
                            error,
                        ),
                    )
                return {"batch_id": batch_id}

            receipt = self.rooms.mutation(principal, "room_connection_rebind_batch", args, perform)
            return self.status(receipt, principal)

    def status(self, raw, principal):
        args = validate(contracts.HostRebindStatus, raw)
        principal = self.owner(principal)
        batch = self.store.one(
            """SELECT * FROM room_host_rebind_batches
            WHERE id=? AND space_id=? AND owner_user_id=?""",
            (args["batch_id"], principal.space_id, principal.user_id),
        )
        if not batch:
            raise DevError("ROOM_CONNECTION_BATCH_NOT_FOUND", "没有这个批量接入流程", 404)
        cursor_scope = ["host_rebinds", batch["id"], principal.space_id, principal.user_id]
        offset = read_cursor(self.c.secret, cursor_scope, args["cursor"]) if args["cursor"] else 0
        items = self.store.all(
            """SELECT i.*, p.status AS candidate_status, p.binding_generation,
            p.prepare_expires_at, r.title AS room_title FROM room_host_rebind_items i
            LEFT JOIN room_pending_bindings p ON p.id=i.candidate_id
            LEFT JOIN conversation_rooms r ON r.id=i.conversation_id
            WHERE i.batch_id=? ORDER BY i.position""",
            (batch["id"],),
        )
        views = [self.item_view(item) for item in items]
        return {
            "batch_id": batch["id"],
            "host_connection_id": batch["host_connection_id"],
            "items": views[offset : offset + args["limit"]],
            "complete": all(item["status"] in {"activated", "failed"} for item in views),
            "next_cursor": sign_cursor(self.c.secret, cursor_scope, offset + args["limit"])
            if len(views) > offset + args["limit"]
            else None,
        }

    def item_view(self, row):
        status = row["candidate_status"]
        error = row["error_code"]
        if not error and status != "activated":
            if status not in {"invited", "bound", "verifying"}:
                error = "ROOM_BINDING_CANCELLED"
            elif row["prepare_expires_at"] <= self.c.clock():
                error = "ROOM_BINDING_EXPIRED"
        return {
            "member_id": row["member_id"],
            "conversation_id": row["conversation_id"],
            "room_title": row["room_title"],
            "flow_id": row["flow_id"],
            "candidate_id": row["candidate_id"],
            "binding_generation": row["binding_generation"],
            "status": "failed" if error else status,
            "error_code": error or None,
        }
