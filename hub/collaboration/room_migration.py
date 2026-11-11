"""Explicit per-room write-plane cutover, separate from connection verification.

Preparing or verifying one member never enables the new room write surface.
The owner commits the cutover only after every retained binding is verified;
old leases and notification routes are retired without deleting their receipts.
"""

from __future__ import annotations

import json

from hub.collaboration.common import ROOM_INBOX_EVENT, canonical, digest
from shared.util import DevError


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_migrations (
        conversation_id TEXT PRIMARY KEY REFERENCES conversation_rooms(id),
        owner_user_id TEXT NOT NULL,from_version INTEGER NOT NULL,to_version INTEGER NOT NULL,
        members TEXT NOT NULL,retired_routes TEXT NOT NULL,activated_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS room_legacy_member_links (
        dot_id TEXT PRIMARY KEY REFERENCES collaboration_dots(id),
        member_id TEXT NOT NULL REFERENCES room_members(id),
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id))""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS room_legacy_link_conversation ON room_legacy_member_links(conversation_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS room_legacy_link_member ON room_legacy_member_links(member_id)"
    )
    for table in ("weixin_bindings", "telegram_bindings"):
        if "conversation_id" in {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}:
            db.execute(
                "CREATE INDEX IF NOT EXISTS room_migration_"
                + table
                + " ON "
                + table
                + "(conversation_id,state)"
            )
    # This is an identity mapping, never an approval or a protocol transition.
    # A migrated member's legacy_dot_id may now point at its private adapter.
    private = (
        " AND NOT EXISTS (SELECT 1 FROM room_member_work_bridges b WHERE b.legacy_dot_id=d.id)"
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='room_member_work_bridges'"
        ).fetchone()
        else ""
    )
    after = ""
    while rows := db.execute(
        """SELECT d.id,d.conversation_id FROM collaboration_dots d
            WHERE d.id>? AND NOT EXISTS (SELECT 1 FROM room_legacy_member_links l WHERE l.dot_id=d.id)"""
        + private
        + " ORDER BY d.id LIMIT 64",
        (after,),
    ).fetchall():
        for dot in rows:
            member_id = "member_" + digest(["legacy", dot[0]])[:32]
            if db.execute("SELECT 1 FROM room_members WHERE id=?", (member_id,)).fetchone():
                db.execute(
                    "INSERT OR IGNORE INTO room_legacy_member_links VALUES (?,?,?)",
                    (dot[0], member_id, dot[1]),
                )
        after = rows[-1][0]


class RoomMigration:
    def __init__(self, rooms):
        self.rooms, self.c, self.store = rooms, rooms.c, rooms.store

    def legacy_write(self, conversation_id):
        """Only legacy business admission calls this; reads and stops do not."""
        room = self.store.one(
            "SELECT room_protocol FROM conversation_rooms WHERE id=?", (conversation_id,)
        )
        if room and room["room_protocol"]:
            raise DevError(
                "ROOM_LEGACY_WRITE_DISABLED",
                "房间已切换到新协作中心，请在当前房间继续；旧入口只保留历史和安全停止",
                409,
            )

    def member_requires_migration(self, member):
        # Native invited members may already own a private work adapter. That
        # adapter ID is not evidence that they came from the old write surface.
        return member["status"] == "legacy" or (
            not member["connection_proof_hash"]
            and bool(
                self.store.one(
                    """SELECT 1 FROM room_legacy_member_links
                WHERE member_id=? AND conversation_id=? LIMIT 1""",
                    (member["id"], member["conversation_id"]),
                )
            )
        )

    def adapter_write(
        self,
        conversation_id,
        member_id,
        project_id,
        environment_id,
        capabilities,
        execution_targets,
        *,
        dot_id="",
    ):
        """Private bridge admission; member_id is never a public wire field."""
        if not member_id:
            self.legacy_write(conversation_id)
            return
        member = self.rooms.member(member_id, conversation_id)
        scope = self.store.one(
            """SELECT * FROM room_member_scopes WHERE member_id=?
            AND project_id=? AND environment_id=?""",
            (member_id, project_id, environment_id),
        )
        if (
            member["status"] in {"legacy", "removed"}
            or not scope
            or not self.rooms.member_scope_authorized(scope)
            or not set(capabilities) <= set(json.loads(scope["capabilities"]))
            or "execute" in capabilities
            and not set(execution_targets) <= set(json.loads(scope["execution_targets"]))
        ):
            raise DevError(
                "ROOM_ADAPTER_SCOPE_CHANGED", "内部工作连接必须属于此成员当前明确批准的原范围", 403
            )
        if dot_id and not self.store.one(
            """SELECT 1 FROM room_member_work_bridges
                WHERE legacy_dot_id=? AND member_id=? AND project_id=? AND environment_id=?
                AND binding_generation=? AND state='active' """,
            (dot_id, member_id, project_id, environment_id, member["binding_generation"]),
        ):
            raise DevError("ROOM_ADAPTER_SCOPE_CHANGED", "内部工作连接不属于当前成员世代", 403)

    def track_legacy_dot(self, dot_id):
        """Called by an explicit old dot create/join, never by a passive read."""
        dot = self.store.one(
            """SELECT d.id,d.conversation_id,j.label,j.grant_id,j.state,j.principal,
            d.created,r.owner_user_id FROM collaboration_dots d JOIN collaboration_join_slots j ON j.id=d.id
            JOIN conversation_rooms r ON r.id=d.conversation_id WHERE d.id=?""",
            (dot_id,),
        )
        if not dot or self.store.one(
            "SELECT 1 FROM room_member_work_bridges WHERE legacy_dot_id=?", (dot_id,)
        ):
            return
        identifier = "member_" + digest(["legacy", dot_id])[:32]
        self.store.execute(
            """INSERT OR IGNORE INTO room_members
            (id,conversation_id,owner_user_id,name,status,grant_id,principal,legacy_dot_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                dot["conversation_id"],
                dot["owner_user_id"],
                dot["label"],
                "removed" if dot["state"] == "revoked" else "legacy",
                dot["grant_id"] or "",
                dot["principal"] or "{}",
                dot_id,
                dot["created"],
                dot["created"],
            ),
        )
        self.store.execute(
            """UPDATE room_members SET grant_id=?,principal=?,updated_at=?
            WHERE id=? AND status='legacy' AND connection_proof_hash='' """,
            (dot["grant_id"] or "", dot["principal"] or "{}", self.c.clock(), identifier),
        )
        self.store.execute(
            """INSERT OR IGNORE INTO room_connect_flows
            (id,conversation_id,member_id,owner_user_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
            (
                "flow_" + digest(["legacy", dot_id])[:32],
                dot["conversation_id"],
                identifier,
                dot["owner_user_id"],
                "legacy",
                dot["created"],
                dot["created"],
            ),
        )
        self.store.execute(
            "INSERT OR IGNORE INTO room_legacy_member_links VALUES (?,?,?)",
            (dot_id, identifier, dot["conversation_id"]),
        )
        self.rooms.change(dot["conversation_id"], "legacy_connection", identifier)

    def old_dots(self, conversation_id, *, blockers=False, after="", limit=64):
        # Historical private adapters are excluded too: superseding one does
        # not turn it into an independently managed legacy connection.
        condition = " AND (l.member_id IS NULL OR j.state!='revoked')" if blockers else ""
        return self.store.all(
            """SELECT d.id,j.state,j.expires_at,l.member_id FROM collaboration_dots d
            JOIN collaboration_join_slots j ON j.id=d.id
            LEFT JOIN room_legacy_member_links l ON l.dot_id=d.id
            WHERE d.conversation_id=? AND d.id>? AND NOT EXISTS
            (SELECT 1 FROM room_member_work_bridges b WHERE b.legacy_dot_id=d.id)"""
            + condition
            + " ORDER BY d.id LIMIT ?",
            (conversation_id, after, limit),
        )

    def verified(self, room, member):
        if (
            member["status"] not in {"bound", "paused"}
            or not member["connection_proof_hash"]
            or not member["probe_read_at"]
            or not member["probe_replied_at"]
            or not member["subscription_verified_at"]
        ):
            return False
        try:
            actor = self.rooms.principal(member)
            self.c.notification_reader(actor)
            self.c.conversations.object(actor, room["id"])
            connections = getattr(self.c, "room_connections", None)
            if connections is not None:
                connections.guard_join(member, actor)
        except DevError:
            return False
        sub = self.store.one(
            "SELECT * FROM mcp_event_subscriptions WHERE id=?", (member["subscription_id"],)
        )
        states = {"active", "paused"} if member["status"] == "paused" else {"active"}
        if (
            not sub
            or sub["state"] not in states
            or sub["expires_at"] <= self.c.clock()
            or sub["name"] != ROOM_INBOX_EVENT
            or sub["grant_id"] != actor.grant_id
            or sub["conversation_id"] != room["id"]
        ):
            return False
        return json.loads(sub["arguments"]) == {
            "owner_user_id": room["owner_user_id"],
            "conversation_id": room["id"],
            "member_id": member["id"],
            "binding_generation": member["binding_generation"],
        }

    def projection(self, room):
        receipt = self.store.one(
            "SELECT activated_at FROM room_migrations WHERE conversation_id=?", (room["id"],)
        )
        if room["room_protocol"]:
            return {
                "required": False,
                "status": "complete" if receipt else "not_required",
                "eligible": False,
                "blockers": [],
                "member_counts": None,
                "eligibility_checked": False,
                "activated_at": receipt["activated_at"] if receipt else None,
            }
        totals = self.store.one(
            """SELECT COUNT(*) total,COALESCE(SUM(status='removed'),0) removed
            FROM room_members WHERE conversation_id=?""",
            (room["id"],),
        )
        members = self.store.all(
            "SELECT * FROM room_members WHERE conversation_id=? AND status!='removed' ORDER BY created_at,id LIMIT 33",
            (room["id"],),
        )
        counts = {
            "total": totals["total"],
            "verified": 0,
            "removed": totals["removed"],
            "pending": 0,
        }
        blockers = []
        if len(members) > 32:
            blockers.append(
                {
                    "code": "ROOM_MEMBER_LIMIT",
                    "title": "新房间最多保留 32 个成员，请先明确移除不再使用的原连接。",
                }
            )
            members = []
            counts.update(verified=None, pending=None)
        if room["state"] != "active":
            blockers.append(
                {
                    "code": "ROOM_ARCHIVED" if room["state"] == "archived" else "ROOM_PAUSED",
                    "title": "请先明确恢复此房间；恢复后仍保持只读，直到完成迁移。",
                }
            )
        for member in members:
            pending = self.rooms.pending.current(member["id"])
            if pending:
                counts["pending"] += 1
                blockers.append(
                    {
                        "code": "MEMBER_BINDING_PENDING",
                        "member_id": member["id"],
                        "title": member["name"] + " 的候选连接尚未完成验证，请继续验证或明确移除。",
                    }
                )
            legacy = member["status"] == "legacy" or not member["connection_proof_hash"]
            if legacy:
                blockers.append(
                    {
                        "code": "MEMBER_MIGRATION_REQUIRED",
                        "member_id": member["id"],
                        "title": member["name"] + " 仍使用原连接，请明确迁移或移除。",
                    }
                )
            elif self.verified(room, member):
                counts["verified"] += 1
            else:
                blockers.append(
                    {
                        "code": "MEMBER_CONNECTION_UNVERIFIED",
                        "member_id": member["id"],
                        "title": member["name"] + " 的当前连接需完成有效订阅、通知读取和回复验证。",
                    }
                )
        old_routes = self.old_dots(room["id"], blockers=True, limit=33)
        truncated = len(old_routes) == 33
        for dot in old_routes:
            # A revoked/expired route has no new-work admission. Its residual
            # subscription can be retired by the explicit cutover transaction.
            blockers.append(
                {
                    "code": "LEGACY_ROUTE_ACTIVE",
                    "member_id": dot["member_id"] or "",
                    "title": "原 dot 路线尚未完成迁移或明确撤销。",
                }
            )
        for channel, table in (("weixin", "weixin_bindings"), ("telegram", "telegram_bindings")):
            native = " AND COALESCE(native_member_id,'')=''" if channel == "weixin" else ""
            bindings = self.store.all(
                "SELECT id,state,version FROM "
                + table
                + " WHERE conversation_id=? AND state NOT IN ('paused','revoked')"
                + native
                + " ORDER BY id LIMIT 17",
                (room["id"],),
            )
            truncated |= len(bindings) == 17
            for binding in bindings:
                blockers.append(
                    {
                        "code": "LEGACY_CHANNEL_ACTIVE",
                        "channel": channel,
                        "binding_id": binding["id"],
                        "binding_version": binding["version"],
                        "remediation": {
                            "method": "POST",
                            "path": "/api/collaboration/rooms/"
                            + room["id"]
                            + "/legacy-channels/"
                            + binding["id"]
                            + "/pause",
                            "body": {"channel": channel, "expected_version": binding["version"]},
                        },
                        "title": "请先安全暂停原"
                        + ("微信" if channel == "weixin" else "Telegram")
                        + "连接；账号、历史和原投递回执会保留。",
                    }
                )
        return {
            "required": True,
            "status": "blocked" if blockers else "ready",
            "eligible": not blockers,
            "blockers": blockers,
            "eligibility_checked": True,
            "blockers_truncated": truncated,
            "member_counts": counts,
            "activated_at": None,
        }

    def activate(self, args, principal, room):
        # mutation() checks the immutable successful receipt first. A different
        # key must still carry the current version and recheck all boundaries.
        room = self.store.one("SELECT * FROM conversation_rooms WHERE id=?", (room["id"],))
        self.rooms.check_version(room, args["expected_version"])
        if room["room_protocol"]:
            return {"conversation_id": room["id"], "activated": False}
        state = self.projection(room)
        if not state["eligible"]:
            raise DevError(
                "ROOM_MIGRATION_BLOCKED",
                "房间仍有未完成的迁移条件，请逐项处理后再明确激活",
                409,
                migration=state,
            )
        now = self.c.clock()
        retired, retired_count = [], 0
        # Stop only subscriptions for this exact conversation or its original
        # dots. Project-wide subscriptions and private adapters remain intact.
        after = ""
        while subscriptions := self.store.all(
            """SELECT id FROM mcp_event_subscriptions
                WHERE conversation_id IS NULL AND state IN ('active','paused') AND
                (json_extract(arguments,'$.conversation_id')=?
                 OR json_extract(arguments,'$.dot_id') IN (SELECT dot_id FROM room_legacy_member_links WHERE conversation_id=?)
                 OR json_extract(arguments,'$.slot_id') IN (SELECT dot_id FROM room_legacy_member_links WHERE conversation_id=?))
                AND id>? ORDER BY id LIMIT 64""",
            (room["id"], room["id"], room["id"], after),
        ):
            for sub in subscriptions:
                self.store.execute(
                    "UPDATE mcp_event_subscriptions SET state='unsubscribed',version=version+1,updated=? WHERE id=?",
                    (now, sub["id"]),
                )
                retired_count += 1
                if len(retired) < 32:
                    retired.append(sub["id"])
            after = subscriptions[-1]["id"]
        after = ""
        while old_dots := self.old_dots(room["id"], after=after):
            for dot in old_dots:
                self.store.execute(
                    "UPDATE dot_consumer_sessions SET state='expired',updated=? WHERE dot_id=? AND member_id IS NULL AND state='active'",
                    (now, dot["id"]),
                )
            after = old_dots[-1]["id"]
        self.store.execute(
            "UPDATE conversation_writers SET enabled=0,version=version+1,updated=? WHERE conversation_id=? AND enabled=1",
            (now, room["id"]),
        )
        changed = self.store.execute(
            """UPDATE conversation_rooms SET room_protocol=1,version=version+1
            WHERE id=? AND room_protocol=0 AND version=?""",
            (room["id"], room["version"]),
        )
        if changed.rowcount != 1:
            raise DevError("STALE_VERSION", "房间迁移状态已改变，请重新核对", 409)
        members = self.store.all(
            "SELECT id,binding_generation,status FROM room_members WHERE conversation_id=? AND status!='removed' ORDER BY id LIMIT 32",
            (room["id"],),
        )
        self.store.execute(
            "INSERT INTO room_migrations VALUES (?,?,?,?,?,?,?)",
            (
                room["id"],
                principal.user_id,
                room["version"],
                room["version"] + 1,
                canonical(members),
                canonical(
                    {
                        "count": retired_count,
                        "sample_ids": retired,
                        "sample_complete": retired_count <= 32,
                    }
                ),
                now,
            ),
        )
        self.rooms.change(room["id"], "migration", room["id"], room["version"] + 1)
        for member in members:
            if member["status"] == "bound":
                self.c.room_consumers.binding_activated(self.rooms.member(member["id"]))
        return {"conversation_id": room["id"], "activated": True}
