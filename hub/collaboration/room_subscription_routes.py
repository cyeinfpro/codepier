"""Same-host callback changes verified independently of the current room route."""

from __future__ import annotations

import json
import secrets

from hub.collaboration import network
from hub.collaboration.common import ROOM_INBOX_EVENT, canonical, digest, validate
from shared.room_contracts import HostRoutePrepare
from shared.util import DevError


ROUTE_LIFETIME = 900


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_subscription_routes (
        id TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES room_members(id),
        binding_generation INTEGER NOT NULL, base_route_generation INTEGER NOT NULL,
        host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
        host_revocation_epoch INTEGER NOT NULL, endpoint_digest TEXT NOT NULL,
        old_subscription_id TEXT NOT NULL, subscription_id TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'approved', expires_at REAL NOT NULL,
        control_key TEXT NOT NULL DEFAULT '', control_handle_id TEXT NOT NULL DEFAULT '',
        rotate_control_handle INTEGER NOT NULL DEFAULT 0, ticket_hash TEXT UNIQUE, ticket_secret TEXT,
        nonce_hash TEXT UNIQUE, nonce_secret TEXT, probe_read_at REAL)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(room_subscription_routes)")}
    for name, declaration in (
        ("control_handle_id", "TEXT NOT NULL DEFAULT ''"),
        ("rotate_control_handle", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE room_subscription_routes ADD COLUMN {name} {declaration}")
    db.execute("""CREATE INDEX IF NOT EXISTS room_subscription_route_current
        ON room_subscription_routes(member_id, state, expires_at)""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_subscription_route_subscription
        ON room_subscription_routes(subscription_id) WHERE subscription_id!=''""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_subscription_route_host
        ON room_subscription_routes(host_connection_id,id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_subscription_route_expiry
        ON room_subscription_routes(expires_at,id) WHERE state IN ('approved','controlled','verifying')""")


class RoomSubscriptionRoutes:
    def __init__(self, rooms):
        self.rooms, self.c, self.store = rooms, rooms.c, rooms.store

    def view(self, row):
        state = row["state"]
        if state in {"approved", "controlled", "verifying"} and row["expires_at"] <= self.c.clock():
            state = "expired"
        return {
            "owner_operation_id": row["id"],
            "member_id": row["member_id"],
            "state": state,
            "expires_at": row["expires_at"],
            "rotate_control_handle": bool(row["rotate_control_handle"]),
            "binding_generation": row["binding_generation"],
            "route_generation": row["base_route_generation"] + int(row["state"] == "active"),
        }

    def prepare(self, raw, principal):
        args = validate(HostRoutePrepare, raw)
        with self.store.transaction():
            owner, room = self.rooms.owner(principal, args["conversation_id"])
            member = self.rooms.member(args["member_id"], room["id"])
            identifier = "route_" + digest([owner.user_id, member["id"], args["idempotency_key"]])
            endpoint = digest(args["callback_url"])
            previous = self.get(identifier)
            if previous:
                if (
                    previous["base_route_generation"] != args["expected_route_generation"]
                    or previous["endpoint_digest"] != endpoint
                    or bool(previous["rotate_control_handle"]) != args["rotate_control_handle"]
                ):
                    raise DevError("IDEMPOTENCY_CONFLICT", "原路线轮换请求参数已经改变", 409)
                return self.view(previous)
            network.callback_url(args["callback_url"])
            if not self.rooms.host_connections_enabled():
                raise DevError("ROOM_CONNECTION_FEATURE_DISABLED", "独立连接接入当前未开启", 409)
            if (
                member["status"] != "bound"
                or room["state"] != "active"
                or not member["probe_replied_at"]
                or not member["subscription_id"]
                or not member.get("host_connection_id")
            ):
                raise DevError("ROOM_ROUTE_UNAVAILABLE", "先恢复当前宿主连接再轮换路线", 409)
            if (
                member["route_generation"] != args["expected_route_generation"]
                or member["endpoint_digest"] == endpoint
            ):
                raise DevError("ROOM_ROUTE_CHANGED", "请核对当前路线并选择新的回调地址", 409)
            host = self.c.room_connections.require_member(room, member)
            candidates = self.store.all(
                """SELECT * FROM room_subscription_routes
                WHERE member_id=? AND state IN ('approved','controlled','verifying')""",
                (member["id"],),
            )
            for candidate in candidates:
                self.end_candidate(
                    candidate, member["subscription_id"], "cancelled", self.c.clock()
                )
            self.store.execute(
                """INSERT INTO room_subscription_routes
                (id,member_id,binding_generation,base_route_generation,host_connection_id,host_revocation_epoch,
                 endpoint_digest,old_subscription_id,expires_at,rotate_control_handle)
                 VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    member["id"],
                    member["binding_generation"],
                    member["route_generation"],
                    host["id"],
                    host["host_revocation_epoch"],
                    endpoint,
                    member["subscription_id"],
                    self.c.clock() + ROUTE_LIFETIME,
                    int(args["rotate_control_handle"]),
                ),
            )
            return self.view(self.get(identifier))

    def retire_subscription(self, subscription_id, now, reason):
        self.store.execute(
            """UPDATE mcp_event_subscriptions SET state='unsubscribed',version=version+1,updated=?
            WHERE id=? AND state IN ('active','paused','expired')""",
            (now, subscription_id),
        )
        self.store.execute(
            """UPDATE mcp_event_deliveries SET state='abandoned',reason_code=?,
            fence=fence+1 WHERE subscription_id=? AND state IN ('pending','retry_wait')""",
            (reason, subscription_id),
        )
        self.store.execute(
            """UPDATE mcp_event_deliveries SET reason_code=?
            WHERE subscription_id=? AND state='leased'""",
            (reason + "_in_flight", subscription_id),
        )

    def end_candidate(self, candidate, current_subscription_id, state, now):
        if candidate["subscription_id"] and candidate["subscription_id"] != current_subscription_id:
            self.retire_subscription(candidate["subscription_id"], now, "route_candidate_ended")
        self.store.execute(
            """UPDATE room_subscription_routes SET state=?,ticket_secret=NULL,nonce_secret=NULL
            WHERE id=?""",
            (state, candidate["id"]),
        )

    def expire(self, now):
        for candidate in self.store.all(
            """SELECT c.*,m.subscription_id AS live_subscription_id
            FROM room_subscription_routes c JOIN room_members m ON m.id=c.member_id
            WHERE c.state IN ('approved','controlled','verifying') AND c.expires_at<=?
            ORDER BY c.expires_at,c.id LIMIT 32""",
            (now,),
        ):
            self.end_candidate(candidate, candidate["live_subscription_id"], "expired", now)

    def get(self, identifier):
        return self.store.one("SELECT * FROM room_subscription_routes WHERE id=?", (identifier,))

    def current(self, row, room, member):
        if (
            not row
            or row["expires_at"] <= self.c.clock()
            or row["state"] not in {"approved", "controlled", "verifying"}
            or row["member_id"] != member["id"]
            or row["binding_generation"] != member["binding_generation"]
            or row["base_route_generation"] != member["route_generation"]
            or row["old_subscription_id"] != member["subscription_id"]
            or row["host_connection_id"] != member["host_connection_id"]
            or room["state"] != "active"
            or member["status"] != "bound"
        ):
            raise DevError("ROOM_ROUTE_CHANGED", "候选路线已经失效，当前路线保持原状", 409)
        return self.c.room_connections.require_member(
            room, member, expected_epoch=row["host_revocation_epoch"]
        )

    def control(self, args, principal):
        with self.store.transaction():
            row = self.get(args["owner_operation_id"])
            if not row or not args.get("control_handle"):
                raise DevError(
                    "ROOM_CONNECTION_CONTROL_INVALID", "缺少原路线操作或受限宿主控制证明", 403
                )
            member = self.rooms.member(row["member_id"])
            principal, room, member = self.rooms.member_scope(
                principal, member["conversation_id"], member["id"], row["binding_generation"]
            )
            host = self.c.room_connections.require_member(
                room, member, expected_epoch=row["host_revocation_epoch"]
            )
            if host["id"] != row["host_connection_id"]:
                raise DevError("ROOM_ROUTE_CHANGED", "当前成员绑定已变化", 409)
            handle = self.rooms.host_proofs.authorize_handle(
                principal, host, args["control_handle"], replay=bool(row["control_key"])
            )
            if row["control_key"]:
                if row["control_key"] != args["idempotency_key"] or (
                    row["control_handle_id"] and row["control_handle_id"] != handle["id"]
                ):
                    raise DevError("ROOM_ROUTE_CONTROLLED", "此候选已经受理原控制请求", 409)
                return self.view(row)
            self.current(row, room, member)
            self.store.execute(
                """UPDATE room_subscription_routes
                SET state='controlled',control_key=?,control_handle_id=? WHERE id=? AND state='approved'""",
                (args["idempotency_key"], handle["id"], row["id"]),
            )
            return self.view(self.get(row["id"]))

    def for_endpoint(self, room, member, endpoint):
        row = self.store.one(
            """SELECT * FROM room_subscription_routes WHERE member_id=? AND endpoint_digest=?
            AND state IN ('controlled','verifying') AND expires_at>? ORDER BY expires_at DESC LIMIT 1""",
            (member["id"], endpoint, self.c.clock()),
        )
        if not row:
            raise DevError("ROOM_HOST_CHANGED", "先批准并验证同一宿主的新通知路线", 409)
        self.current(row, room, member)
        return row

    def for_subscription(self, identifier):
        return self.store.one(
            """SELECT * FROM room_subscription_routes
            WHERE subscription_id=? AND state='verifying' LIMIT 1""",
            (identifier,),
        )

    def attach(self, row, member, subscription):
        if row["state"] == "verifying":
            if row["subscription_id"] != subscription["id"]:
                raise DevError("ROOM_ROUTE_CHANGED", "候选路线的订阅身份发生变化", 409)
            return
        ticket, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.store.execute(
            """UPDATE room_subscription_routes SET state='verifying',subscription_id=?,
            ticket_hash=?,ticket_secret=?,nonce_hash=?,nonce_secret=? WHERE id=? AND state='controlled'""",
            (
                subscription["id"],
                digest(ticket),
                self.store.encrypt(ticket),
                digest(nonce),
                self.store.encrypt(nonce),
                row["id"],
            ),
        )
        payload = {
            "schema_version": 1,
            "owner_user_id": member["owner_user_id"],
            "conversation_id": member["conversation_id"],
            "member_id": member["id"],
            "binding_generation": member["binding_generation"],
            "dirty_revision": 0,
            "route_probe_id": row["id"],
            "connection_check_required": True,
        }
        event_id = "route_probe_" + digest(row["id"])
        self.store.execute(
            """INSERT INTO mcp_event_outbox
            (id,room_id,conversation_id,name,object_id,object_version,target_grant_id,data,created)
            VALUES (?,NULL,?,?,?,?,?,?,?)""",
            (
                event_id,
                member["conversation_id"],
                ROOM_INBOX_EVENT,
                event_id,
                1,
                member["grant_id"],
                canonical(payload),
                self.c.clock(),
            ),
        )
        if getattr(self.c, "loops", None) is not None:
            self.store.after_commit(self.c.loops.wake_events)

    def authorize_delivery(self, subscription, room, member, payload):
        row = self.for_subscription(subscription["id"])
        self.current(row, room, member)
        if payload is not None and payload.get("route_probe_id") != row["id"]:
            raise DevError("ROOM_ROUTE_PROBE_ONLY", "候选订阅只允许投递其专用路线探针", 403)
        return row

    def payload(self, subscription, payload, member):
        room = self.store.one(
            "SELECT * FROM conversation_rooms WHERE id=?", (member["conversation_id"],)
        )
        row = self.authorize_delivery(subscription, room, member, payload)
        return {**payload, "wake_ticket": self.store.decrypt(row["ticket_secret"])}

    def probe_read(self, raw, room, member):
        row = self.store.one(
            "SELECT * FROM room_subscription_routes WHERE ticket_hash=?",
            (digest(raw["wake_ticket"]),),
        )
        if not row:
            return None
        self.current(row, room, member)
        self.probe_subscription(row)
        self.store.execute(
            "UPDATE room_subscription_routes SET probe_read_at=COALESCE(probe_read_at,?) WHERE id=?",
            (self.c.clock(), row["id"]),
        )
        return {
            "member_id": member["id"],
            "conversation_id": room["id"],
            "binding_generation": member["binding_generation"],
            "probe_nonce": self.store.decrypt(row["nonce_secret"]),
            "purpose": "route_connection_check",
            "business_task_created": False,
            "grants_authority": False,
        }

    def probe_subscription(self, row):
        sub = self.store.one(
            "SELECT * FROM mcp_event_subscriptions WHERE id=?", (row["subscription_id"],)
        )
        if not sub or sub["state"] != "active" or sub["expires_at"] <= self.c.clock():
            raise DevError("ROOM_ROUTE_SUBSCRIPTION_REQUIRED", "候选订阅需要真实续订", 409)
        return sub

    def probe_result(self, row, raw, principal, *, establish=False):
        result = self.view(row)
        if row["rotate_control_handle"]:
            handle = self.rooms.host_proofs.rotate_handle(principal, row, raw, establish=establish)
            if handle:
                result["control_handle"] = handle
        return result

    def probe_reply(self, raw, room, member, principal):
        row = self.store.one(
            "SELECT * FROM room_subscription_routes WHERE nonce_hash=?",
            (digest(raw["probe_nonce"]),),
        )
        if not row:
            return None
        if (
            row["state"] == "active"
            and row["member_id"] == member["id"]
            and row["binding_generation"] == member["binding_generation"]
            and row["subscription_id"] == member["subscription_id"]
        ):
            return self.probe_result(row, raw, principal)
        self.current(row, room, member)
        sub = self.probe_subscription(row)
        if not row["probe_read_at"]:
            raise DevError("ROOM_ROUTE_PROBE_REQUIRED", "先用候选通知票据读取探针", 409)
        now = self.c.clock()
        changed = self.store.execute(
            """UPDATE room_members SET subscription_id=?,endpoint_digest=?,credential_digest=?,
            route_generation=route_generation+1,subscription_verified_at=?,version=version+1,updated_at=?
            WHERE id=? AND binding_generation=? AND route_generation=? AND host_connection_id=? AND subscription_id=?""",
            (
                sub["id"],
                row["endpoint_digest"],
                sub["key_digest"],
                now,
                now,
                member["id"],
                row["binding_generation"],
                row["base_route_generation"],
                row["host_connection_id"],
                row["old_subscription_id"],
            ),
        )
        if changed.rowcount != 1:
            raise DevError("ROOM_ROUTE_CHANGED", "候选路线已经失效", 409)
        self.retire_subscription(row["old_subscription_id"], now, "route_rotated")
        self.store.execute(
            "UPDATE room_subscription_routes SET state='active',ticket_secret=NULL,nonce_secret=NULL WHERE id=?",
            (row["id"],),
        )
        member = self.rooms.member(member["id"])
        stored = json.loads(self.store.decrypt(sub["secret"]))
        self.rooms.renewals.attach(
            member, sub, stored["room_ttl_seconds"], authenticated_route_change=True
        )
        self.c.room_connections.touch_health(member["host_connection_id"])
        self.rooms.change(room["id"], "member", member["id"], member["version"])
        self.c.room_consumers.recover_connection(member)
        return self.probe_result(self.get(row["id"]), raw, principal, establish=True)
