"""Owner-approved health checks for a verified native room subscription."""

from __future__ import annotations

import asyncio
import json
import secrets

from hub.collaboration.common import ROOM_INBOX_EVENT, digest
from hub.collaboration.event_errors import CallbackEndpointError
from hub.grant_expiry import migrate as migrate_grant_expiry, policy as grant_policy
from shared.util import DevError


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_subscription_renewals (
        member_id TEXT PRIMARY KEY REFERENCES room_members(id) ON DELETE CASCADE,
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),
        owner_user_id TEXT NOT NULL, binding_generation INTEGER NOT NULL,
        subscription_id TEXT NOT NULL DEFAULT '', grant_id TEXT NOT NULL DEFAULT '',
        endpoint_digest TEXT NOT NULL DEFAULT '', credential_digest TEXT NOT NULL DEFAULT '',
        approved_until REAL, ttl_seconds REAL, next_at REAL,
        state TEXT NOT NULL DEFAULT 'approved', reason_code TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1)""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_subscription_renewals_due
        ON room_subscription_renewals(next_at,member_id) WHERE state='approved' AND next_at IS NOT NULL""")
    # Some startup paths initialize collaboration before the grant policy journal.
    migrate_grant_expiry(db)
    # A later extension cannot restore approval beyond any earlier narrowing.
    for suffix, operation in (("insert", "INSERT"), ("update", "UPDATE OF expires")):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS room_renewal_grant_{suffix}
            AFTER {operation} ON grant_expirations WHEN NEW.expires IS NOT NULL BEGIN
            UPDATE room_subscription_renewals SET approved_until=NEW.expires,
                next_at=MIN(next_at,NEW.expires),version=version+1
            WHERE grant_id=NEW.grant_id AND state='approved'
                AND (approved_until IS NULL OR approved_until>NEW.expires);
            END""")


def next_renewal(expires, ttl):
    return expires - min(1800, max(15, ttl / 4))


class RoomSubscriptionRenewals:
    def __init__(self, rooms):
        self.rooms, self.c, self.store = rooms, rooms.c, rooms.store

    def record(self, member):
        return self.store.one(
            "SELECT * FROM room_subscription_renewals WHERE member_id=?", (member["id"],)
        )

    def grant_end(self, grant_id):
        policy = grant_policy(self.store, grant_id) if grant_id else None
        return policy["expires"] if policy else None

    def view(self, member):
        row = self.record(member)
        subscription = (
            self.store.one(
                "SELECT state,expires_at FROM mcp_event_subscriptions WHERE id=?",
                (member["subscription_id"],),
            )
            if member["subscription_id"]
            else None
        )
        expires = subscription["expires_at"] if subscription else None
        now = self.c.clock()
        current = bool(subscription and subscription["state"] == "active" and expires > now)
        awaiting_refresh = bool(
            subscription and subscription["state"] in {"active", "expired"} and expires <= now
        )
        state = "active" if current else "awaiting_host_refresh" if awaiting_refresh else "waiting"
        lifecycle = {"state": state, "expires_at": expires, "renewal_managed_by": "host"}
        if not row or row["binding_generation"] != member["binding_generation"]:
            return {**lifecycle, "mode": "host", "approved": False}
        approval_expired = row["approved_until"] is not None and row["approved_until"] <= now
        approved = row["state"] == "approved" and not approval_expired
        if not approved:
            state = "stopped"
        elif not member["probe_replied_at"]:
            state = "awaiting_probe"
        elif current and row["reason_code"] == "callback_unverified":
            state = "recovering"
        return {
            **lifecycle,
            "mode": "managed",
            "approved": approved,
            "state": state,
            "next_at": row["next_at"],
            "approved_until": row["approved_until"],
            "reason_code": "authorization_expired" if approval_expired else row["reason_code"],
        }

    def set(self, member, owner, enabled):
        if member["owner_user_id"] != owner.user_id or owner.grant_id:
            raise DevError("ROOM_OWNER_REQUIRED", "只有房主能批准保持连接", 403)
        if not enabled:
            self.store.execute(
                "DELETE FROM room_subscription_renewals WHERE member_id=?", (member["id"],)
            )
            return
        self.store.execute(
            """INSERT INTO room_subscription_renewals
            (member_id,conversation_id,owner_user_id,binding_generation,approved_until)
            VALUES (?,?,?,?,?) ON CONFLICT(member_id) DO UPDATE SET
            binding_generation=excluded.binding_generation,approved_until=excluded.approved_until,
            subscription_id='',grant_id='',endpoint_digest='',credential_digest='',ttl_seconds=NULL,
            next_at=NULL,state='approved',reason_code='',attempts=0,version=room_subscription_renewals.version+1""",
            (
                member["id"],
                member["conversation_id"],
                owner.user_id,
                member["binding_generation"],
                self.grant_end(member["grant_id"]),
            ),
        )
        if member["subscription_id"]:
            sub = self.store.one(
                "SELECT * FROM mcp_event_subscriptions WHERE id=?", (member["subscription_id"],)
            )
            if (
                sub
                and sub["state"] in {"active", "paused", "expired"}
                and member["probe_replied_at"]
            ):
                stored = json.loads(self.store.decrypt(sub["secret"]))
                elapsed = sub["expires_at"] - sub["updated"]
                if elapsed <= 0:
                    elapsed = sub["expires_at"] - sub["created"]
                ttl = stored.get("room_ttl_seconds", max(60, min(86400, elapsed)))
                self.attach(member, sub, ttl)

    def stop(self, subscription_id, reason):
        self.store.execute(
            """UPDATE room_subscription_renewals SET state='stopped',next_at=NULL,
            reason_code=?,version=version+1 WHERE subscription_id=? AND state='approved'""",
            (reason, subscription_id),
        )

    def attach(
        self, member, sub, ttl, *, authenticated_rotation=False, authenticated_route_change=False
    ):
        row = self.record(member)
        if (
            not row
            or row["state"] != "approved"
            or row["binding_generation"] != member["binding_generation"]
            or row["conversation_id"] != member["conversation_id"]
            or row["owner_user_id"] != member["owner_user_id"]
            or sub["name"] != ROOM_INBOX_EVENT
        ):
            return
        if row["subscription_id"] and (
            row["grant_id"] != sub["grant_id"]
            or (
                not authenticated_route_change
                and (
                    row["subscription_id"] != sub["id"]
                    or row["endpoint_digest"] != member["endpoint_digest"]
                    or (
                        row["credential_digest"] != sub["key_digest"] and not authenticated_rotation
                    )
                )
            )
        ):
            self.stop(row["subscription_id"], "connection_changed")
            return
        approved_until = (
            row["approved_until"] if row["subscription_id"] else self.grant_end(sub["grant_id"])
        )
        next_at = next_renewal(sub["expires_at"], ttl)
        if approved_until is not None:
            next_at = min(next_at, approved_until)
        self.store.execute(
            """UPDATE room_subscription_renewals SET subscription_id=?,grant_id=?,
            endpoint_digest=?,credential_digest=?,approved_until=?,ttl_seconds=?,next_at=?,
            attempts=0,reason_code='',version=version+1 WHERE member_id=?""",
            (
                sub["id"],
                sub["grant_id"],
                member["endpoint_digest"],
                sub["key_digest"],
                approved_until,
                ttl,
                next_at,
                member["id"],
            ),
        )

    def current(self, events, row, now):
        events.guard()
        member = self.store.one("SELECT * FROM room_members WHERE id=?", (row["member_id"],))
        sub = self.store.one(
            "SELECT * FROM mcp_event_subscriptions WHERE id=?", (row["subscription_id"],)
        )
        room = self.store.one(
            "SELECT * FROM conversation_rooms WHERE id=?", (row["conversation_id"],)
        )
        if (
            not member
            or not sub
            or not room
            or not room["room_protocol"]
            or room["archived_at"] is not None
            or room["owner_user_id"] != row["owner_user_id"]
            or member["owner_user_id"] != row["owner_user_id"]
            or member["conversation_id"] != room["id"]
            or member["status"] in {"removed", "legacy"}
            or member["binding_generation"] != row["binding_generation"]
            or member["subscription_id"] != sub["id"]
            or member["grant_id"] != row["grant_id"]
            or sub["grant_id"] != row["grant_id"]
            or sub["conversation_id"] != room["id"]
            or sub["name"] != ROOM_INBOX_EVENT
            or sub["state"] not in {"active", "expired", "paused"}
            or member["endpoint_digest"] != row["endpoint_digest"]
            or member["credential_digest"] != row["credential_digest"]
            or sub["key_digest"] != row["credential_digest"]
        ):
            raise DevError("ROOM_RENEWAL_STOPPED", "原房间连接或签名已经改变", 410)
        grant = self.store.one(
            "SELECT revoked FROM grants WHERE id=? AND user_id=?",
            (row["grant_id"], row["owner_user_id"]),
        )
        ends = [
            x for x in (row["approved_until"], self.grant_end(row["grant_id"])) if x is not None
        ]
        expires = min(ends, default=None)
        if not grant or grant["revoked"] or (expires is not None and expires <= now):
            raise DevError("ROOM_RENEWAL_STOPPED", "原连接授权已到期或撤销", 410)
        if (
            room["state"] != "active"
            or member["status"] == "paused"
            or sub["state"] == "paused"
            or not member["probe_replied_at"]
            or not member["subscription_verified_at"]
        ):
            raise DevError("ROOM_RENEWAL_WAITING", "等待原房间连接恢复或通过探测", 409)
        events.authorize(sub, {**room, "_event_conversation": True})
        stored = json.loads(self.store.decrypt(sub["secret"]))
        if (
            digest(stored["url"]) != row["endpoint_digest"]
            or digest(stored["current"]) != row["credential_digest"]
        ):
            raise DevError("ROOM_RENEWAL_STOPPED", "原回调身份已变化", 410)
        return member, sub, stored, expires

    def defer(self, row, now, reason, *, before=None):
        delay = min(900, 15 * 2 ** min(row["attempts"], 6))
        delay += secrets.randbelow(max(1, delay // 4))
        next_at = min(now + delay, before) if before is not None else now + delay
        self.store.execute(
            """UPDATE room_subscription_renewals SET next_at=?,attempts=attempts+1,
            reason_code=?,version=version+1 WHERE member_id=? AND version=?""",
            (next_at, reason, row["member_id"], row["version"]),
        )

    def reject(self, row, now, exc):
        if exc.code in {"ROOM_RENEWAL_STOPPED", "ROOM_CONNECTION_REVOKED"} or exc.status in {
            401,
            403,
            404,
            410,
        }:
            self.stop(row["subscription_id"], exc.code)
        else:
            self.defer(row, now, exc.code)

    def wait_for_host(self, row, next_at):
        self.store.execute(
            """UPDATE room_subscription_renewals SET next_at=?,attempts=0,
            reason_code='awaiting_host_refresh',version=version+1 WHERE member_id=? AND version=?""",
            (next_at, row["member_id"], row["version"]),
        )

    def claim(self, events):
        with self.store.transaction():
            now = self.c.clock()
            rows = self.store.all(
                """SELECT * FROM room_subscription_renewals WHERE state='approved'
                AND next_at<=? ORDER BY next_at,member_id LIMIT 4""",
                (now,),
            )
            result = []
            for row in rows:
                try:
                    _, sub, stored, authorized_until = self.current(events, row, now)
                except DevError as exc:
                    self.reject(row, now, exc)
                    continue
                if sub["state"] == "expired" or sub["expires_at"] <= now:
                    self.wait_for_host(row, authorized_until)
                    continue
                # The timestamp is also a crash-recoverable claim lease.
                self.store.execute(
                    """UPDATE room_subscription_renewals SET next_at=?,version=version+1
                    WHERE member_id=? AND version=?""",
                    (now + 45, row["member_id"], row["version"]),
                )
                result.append(
                    (row["member_id"], row["version"] + 1, sub["id"], sub["version"], stored)
                )
            return result

    def finish(self, events, lease, accepted):
        member_id, version, subscription_id, subscription_version, _ = lease
        with self.store.transaction():
            now = self.c.clock()
            row = self.store.one(
                """SELECT * FROM room_subscription_renewals
                WHERE member_id=? AND state='approved' AND version=?""",
                (member_id, version),
            )
            if not row:
                return
            try:
                member, sub, _, authorized_until = self.current(events, row, now)
            except DevError as exc:
                self.reject(row, now, exc)
                return
            if sub["id"] != subscription_id or sub["version"] != subscription_version:
                return
            if sub["state"] == "expired" or sub["expires_at"] <= now:
                self.wait_for_host(row, authorized_until)
            else:
                next_at = (
                    min(sub["expires_at"], authorized_until)
                    if authorized_until is not None
                    else sub["expires_at"]
                )
                if accepted:
                    # A verification response establishes reachability only. Only
                    # a real events/subscribe request can grant another lifetime.
                    self.wait_for_host(row, next_at)
                else:
                    self.defer(row, now, "callback_unverified", before=next_at)
            self.c.room_connections.touch_health(member.get("host_connection_id"))
            self.rooms.change(member["conversation_id"], "member", member_id, member["version"])

    async def tick(self, events):
        async def renew(lease):
            _, _, identifier, _, stored = lease
            try:
                await events.verify_callback(stored["url"], stored["current"], identifier)
                accepted = True
            except CallbackEndpointError:
                accepted = False
            await self.store.run(self.finish, events, lease, accepted)

        leases = await self.store.run(self.claim, events)
        if leases:
            await asyncio.gather(*(renew(lease) for lease in leases))
