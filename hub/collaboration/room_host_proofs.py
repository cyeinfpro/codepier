"""Purpose-limited host control for an owner-approved binding operation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid

from hub.collaboration.common import ROOM_INBOX_EVENT, canonical, validate
from hub.principal import refresh_principal
from shared.room_contracts import HostControl
from shared.util import DevError


CHALLENGE_LIFETIME = 900
HANDLE_LIFETIME = 90 * 86400
HANDLE_REPLAY_WINDOW = 1800


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_host_control_handles (
        id TEXT PRIMARY KEY, host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
        host_revocation_epoch INTEGER NOT NULL, secret_digest TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK(purpose='binding_control'),
        state TEXT NOT NULL CHECK(state IN ('active','retiring','revoked')), expires_at REAL NOT NULL,
        created_by_operation_id TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
        exchange_digest TEXT NOT NULL, exchange_secret TEXT NOT NULL, exchange_expires_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS room_host_attach_challenges (
        id TEXT PRIMARY KEY, host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
        host_revocation_epoch INTEGER NOT NULL, target_member_id TEXT NOT NULL,
        target_generation INTEGER NOT NULL, owner_operation_id TEXT NOT NULL UNIQUE,
        secret_digest TEXT NOT NULL, encrypted_secret TEXT NOT NULL,
        source_subscription_id TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL CHECK(state IN ('pending','consumed','expired','cancelled')),
        expires_at REAL NOT NULL, consumed_by_operation_id TEXT, exchange_digest TEXT,
        created_at REAL NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_host_attach_target
        ON room_host_attach_challenges(target_member_id,target_generation,state)""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_host_attach_expiry
        ON room_host_attach_challenges(expires_at,id) WHERE state='pending' """)
    db.execute("""CREATE INDEX IF NOT EXISTS room_host_handle_exchange_expiry
        ON room_host_control_handles(exchange_expires_at,id) WHERE exchange_secret!='' """)


class HostControlProofs:
    def __init__(self, rooms):
        self.rooms, self.c, self.store = rooms, rooms.c, rooms.store

    def keyed_digest(self, kind, value):
        return hmac.new(
            self.c.secret, canonical([kind, value]).encode(), hashlib.sha256
        ).hexdigest()

    def expire(self, limit=64):
        now = self.c.clock()
        with self.store.transaction():
            self.store.execute(
                """UPDATE room_host_attach_challenges
                SET state=CASE WHEN expires_at<=? THEN 'expired' ELSE 'cancelled' END,encrypted_secret=''
                WHERE id IN (SELECT c.id FROM room_host_attach_challenges c
                    JOIN room_host_connections h ON h.id=c.host_connection_id
                    WHERE c.state='pending' AND (c.expires_at<=? OR h.state='revoked')
                    ORDER BY c.expires_at,c.id LIMIT ?)""",
                (now, now, limit),
            )
            self.store.execute(
                """UPDATE room_host_control_handles SET exchange_secret=''
                WHERE id IN (SELECT c.id FROM room_host_control_handles c
                    JOIN room_host_connections h ON h.id=c.host_connection_id
                    WHERE c.exchange_secret!='' AND (c.exchange_expires_at<=? OR h.state='revoked')
                    ORDER BY c.exchange_expires_at,c.id LIMIT ?)""",
                (now, limit),
            )

    def cancel_target(self, member_id, generation):
        self.store.execute(
            """UPDATE room_host_attach_challenges SET state='cancelled',encrypted_secret=''
            WHERE target_member_id=? AND target_generation=? AND state='pending' """,
            (member_id, generation),
        )

    def source(self, host):
        return self.store.one(
            """SELECT m.* FROM room_members m
            JOIN conversation_rooms r ON r.id=m.conversation_id
            JOIN mcp_event_subscriptions s ON s.id=m.subscription_id
            WHERE m.host_connection_id=? AND m.host_revocation_epoch=?
                AND m.owner_user_id=? AND m.grant_id=? AND m.status='bound'
                AND r.space_id=? AND r.state='active'
                AND m.probe_replied_at IS NOT NULL AND m.subscription_verified_at IS NOT NULL
                AND s.state='active' AND (s.expires_at IS NULL OR s.expires_at>?)
                AND s.grant_id=m.grant_id AND s.name=? AND s.conversation_id=m.conversation_id
            ORDER BY m.probe_replied_at DESC,m.id LIMIT 1""",
            (
                host["id"],
                host["host_revocation_epoch"],
                host["owner_user_id"],
                host["grant_id"],
                host["space_id"],
                self.c.clock(),
                ROOM_INBOX_EVENT,
            ),
        )

    def can_challenge(self, host):
        return host["state"] == "active" and self.source(host) is not None

    def current_host(self, challenge, principal=None):
        host = self.c.room_connections.read_host(challenge["host_connection_id"])
        if not host or (
            principal is not None
            and (host["space_id"], host["owner_user_id"], host["grant_id"])
            != (principal.space_id, principal.user_id, principal.grant_id)
        ):
            raise DevError("ROOM_CONNECTION_IDENTITY_REQUIRED", "需要当前宿主的连接控制证明", 403)
        if (
            host["state"] != "active"
            or host["host_revocation_epoch"] != challenge["host_revocation_epoch"]
        ):
            raise DevError("ROOM_CONNECTION_REVOKED", "这个 dot 的连接已撤销", 409)
        return host

    def prepare(self, owner, host, target, owner_operation_id):
        """Reserve a target and queue its challenge in the owner's transaction."""
        owner, host = self.c.room_connections.owned_host(owner, host["id"])
        self.current_host(
            {
                "host_connection_id": host["id"],
                "host_revocation_epoch": host["host_revocation_epoch"],
            }
        )
        _, room = self.rooms.owner(owner, target["conversation_id"])
        duplicate = self.store.one(
            """SELECT id FROM room_members
            WHERE conversation_id=? AND host_connection_id=? AND status!='removed' AND id!=?
            UNION ALL SELECT m.id FROM room_pending_bindings p JOIN room_members m ON m.id=p.member_id
            WHERE m.conversation_id=? AND p.host_connection_id=? AND m.id!=?
              AND p.status IN ('invited','bound','verifying') AND p.prepare_expires_at>? LIMIT 1""",
            (
                room["id"],
                host["id"],
                target["id"],
                room["id"],
                host["id"],
                target["id"],
                self.c.clock(),
            ),
        )
        if duplicate:
            raise DevError(
                "ROOM_CONNECTION_ALREADY_PRESENT",
                "这个 dot 已有当前房间的接入流程",
                409,
                member_id=duplicate["id"],
            )
        table, identifier = self.rooms.binding_storage(target)
        changed = self.store.execute(
            f"""UPDATE {table} SET host_connection_id=?,host_revocation_epoch=?
            WHERE id=? AND binding_generation=? AND host_connection_id='' """,
            (host["id"], host["host_revocation_epoch"], identifier, target["binding_generation"]),
        )
        if changed.rowcount != 1:
            raise DevError("ROOM_BINDING_CHANGED", "目标成员的接入流程已变化", 409)
        self.store.execute(
            """UPDATE room_host_connections SET authority_version=authority_version+1,updated_at=?
            WHERE id=?""",
            (self.c.clock(), host["id"]),
        )
        source = self.source(host)
        now, challenge_id = self.c.clock(), uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        self.store.execute(
            """INSERT INTO room_host_attach_challenges
            (id,host_connection_id,host_revocation_epoch,target_member_id,target_generation,owner_operation_id,
             secret_digest,encrypted_secret,source_subscription_id,state,expires_at,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (
                challenge_id,
                host["id"],
                host["host_revocation_epoch"],
                target["id"],
                target["binding_generation"],
                owner_operation_id,
                self.keyed_digest("attach", token),
                self.store.encrypt(token),
                source["subscription_id"] if source else "",
                now + CHALLENGE_LIFETIME,
                now,
            ),
        )
        if source:
            self.queue(challenge_id, source, now)
        return challenge_id

    def refresh(self, target, owner_operation_id):
        # Called only from the original owner's explicit flow action. Rotating
        # the challenge ID also invalidates any previously frozen delivery.
        row = self.store.one(
            "SELECT * FROM room_host_attach_challenges WHERE owner_operation_id=?",
            (owner_operation_id,),
        )
        if not row or row["state"] not in {"pending", "expired"}:
            return False
        host = self.current_host(row)
        member, _ = self.target(row)
        if (member["id"], member["binding_generation"]) != (
            target["id"],
            target["binding_generation"],
        ):
            raise DevError("ROOM_BINDING_CHANGED", "目标成员的接入流程已变化", 409)
        source = self.source(host)
        now, challenge_id = self.c.clock(), uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        self.store.execute(
            """UPDATE room_host_attach_challenges SET id=?,secret_digest=?,encrypted_secret=?,
            source_subscription_id=?,state='pending',expires_at=? WHERE id=?""",
            (
                challenge_id,
                self.keyed_digest("attach", token),
                self.store.encrypt(token),
                source["subscription_id"] if source else "",
                now + CHALLENGE_LIFETIME,
                row["id"],
            ),
        )
        if source:
            self.queue(challenge_id, source, now)
        return True

    def queue(self, challenge_id, source, now):
        event_id = "room_host_attach_" + challenge_id
        payload = {
            "schema_version": 1,
            "owner_user_id": source["owner_user_id"],
            "conversation_id": source["conversation_id"],
            "member_id": source["id"],
            "binding_generation": source["binding_generation"],
            "dirty_revision": source["dirty_revision"],
            "host_control_challenge_id": challenge_id,
            "host_control_subscription_id": source["subscription_id"],
        }
        self.store.execute(
            """INSERT OR IGNORE INTO mcp_event_outbox
            (id,room_id,conversation_id,name,object_id,object_version,target_grant_id,data,created)
            VALUES (?,NULL,?,?,?,?,?,?,?)""",
            (
                event_id,
                source["conversation_id"],
                ROOM_INBOX_EVENT,
                event_id,
                1,
                source["grant_id"],
                canonical(payload),
                now,
            ),
        )
        loops = getattr(self.c, "loops", None)
        if loops is not None:
            self.store.after_commit(loops.wake_events)

    def target(self, challenge):
        member = self.rooms.member(challenge["target_member_id"])
        generation = challenge["target_generation"]
        if member["binding_generation"] == generation:
            target = member
            invitation = self.store.one(
                """SELECT * FROM room_connect_flows
                WHERE id=? AND member_id=?""",
                (challenge["owner_operation_id"], member["id"]),
            )
            if not invitation and challenge["state"] == "consumed":
                # The replacement operation ID stays on its original candidate.
                invitation = self.store.one(
                    """SELECT * FROM room_pending_bindings
                    WHERE id=? AND member_id=? AND status='activated' """,
                    (challenge["owner_operation_id"], member["id"]),
                )
        else:
            target = self.rooms.pending.by_generation(member, generation)
            invitation = self.store.one(
                """SELECT * FROM room_pending_bindings
                WHERE id=? AND member_id=?""",
                (challenge["owner_operation_id"], member["id"]),
            )
            if invitation and invitation["id"] != target["_pending_binding_id"]:
                invitation = None
        room = self.store.one(
            "SELECT * FROM conversation_rooms WHERE id=?", (member["conversation_id"],)
        )
        inactive = (
            not self.rooms.can_prepare_replacement(member)
            if target.get("_pending_binding")
            else member["status"] in {"paused", "removed", "legacy"}
        )
        if (
            not invitation
            or invitation["revoked_at"] is not None
            or not room
            or room["state"] != "active"
            or inactive
            or target["host_connection_id"] != challenge["host_connection_id"]
            or target["host_revocation_epoch"] != challenge["host_revocation_epoch"]
        ):
            raise DevError("ROOM_BINDING_CHANGED", "目标成员的接入流程已失效", 409)
        return target, invitation

    def authorize_delivery(self, subscription, payload):
        challenge = self.store.one(
            "SELECT * FROM room_host_attach_challenges WHERE id=?",
            (payload["host_control_challenge_id"],),
        )
        if (
            not challenge
            or challenge["state"] != "pending"
            or challenge["expires_at"] <= self.c.clock()
            or challenge["source_subscription_id"] != subscription["id"]
            or payload.get("host_control_subscription_id") != subscription["id"]
        ):
            raise DevError("ROOM_CONNECTION_CONTROL_EXPIRED", "原连接验证已结束", 409)
        self.current_host(challenge)
        self.target(challenge)
        return challenge

    def delivery_payload(self, subscription, payload):
        from shared.public_collaboration import checked_request

        challenge = self.authorize_delivery(subscription, payload)
        request = checked_request(
            "collaboration_room_connection_control",
            {
                "owner_operation_id": challenge["owner_operation_id"],
                "challenge_proof": self.store.decrypt(challenge["encrypted_secret"]),
                "idempotency_key": "host-attach-" + challenge["id"],
            },
        )
        return {**payload, "wake_ticket": None, "host_control_request": request}

    def authorize_handle(self, principal, host, handle, *, replay=False):
        principal = refresh_principal(self.store, principal)
        self.c.notification_reader(principal)
        self.current_host(
            {
                "host_connection_id": host["id"],
                "host_revocation_epoch": host["host_revocation_epoch"],
            },
            principal,
        )
        identifier, _, token = (handle or "").partition(".")
        row = self.store.one("SELECT * FROM room_host_control_handles WHERE id=?", (identifier,))
        if (
            not row
            or row["host_connection_id"] != host["id"]
            or row["purpose"] != "binding_control"
            or row["host_revocation_epoch"] != host["host_revocation_epoch"]
            or row["expires_at"] <= self.c.clock()
            or (row["state"] != "active" and not (replay and row["state"] == "retiring"))
            or not hmac.compare_digest(row["secret_digest"], self.keyed_digest("handle", token))
        ):
            raise DevError("ROOM_CONNECTION_IDENTITY_REQUIRED", "需要当前宿主的连接控制证明", 403)
        return row

    def issue_handle(self, member, room, principal, verified_args):
        """Issue/recover only in the original authenticated establishment reply."""
        if member["connection_identity_version"] != 2 or not member["host_connection_id"]:
            return None
        host = self.c.room_connections.require_host_current(
            principal, room, member, expected_epoch=member["host_revocation_epoch"]
        )
        if (host["established_by_member_id"], host["established_by_generation"]) != (
            member["id"],
            member["binding_generation"],
        ):
            return None
        adoption = self.store.one(
            """SELECT evidence_kind FROM room_host_binding_adoptions
            WHERE member_id=? AND binding_generation=?""",
            (member["id"], member["binding_generation"]),
        )
        if not adoption or adoption["evidence_kind"] != "verified_probe":
            return None
        operation = host["established_by_operation_id"]
        fingerprint = self.keyed_digest("establishment", [principal.grant_id, verified_args])
        previous = self.handle_exchange(operation)
        if previous:
            return self.recover_handle(previous, fingerprint)
        if self.c.clock() > host["verified_at"] + HANDLE_REPLAY_WINDOW:
            return None
        return self.create_handle(host, operation, fingerprint)

    def handle_exchange(self, operation):
        return self.store.one(
            "SELECT * FROM room_host_control_handles WHERE created_by_operation_id=?", (operation,)
        )

    def recover_handle(self, previous, fingerprint):
        now = self.c.clock()
        if (
            previous["state"] == "active"
            and previous["exchange_secret"]
            and previous["expires_at"] > now
            and previous["exchange_expires_at"] > now
            and hmac.compare_digest(previous["exchange_digest"], fingerprint)
        ):
            return self.store.decrypt(previous["exchange_secret"])
        return None

    def rotate_handle(self, principal, route, verified_args, *, establish=False):
        """Rotate only with the approved route's exact successful round trip."""
        host = self.current_host(route, principal)
        operation = route["id"]
        fingerprint = self.keyed_digest(
            "route-handle-rotation", [principal.grant_id, verified_args]
        )
        previous = self.handle_exchange(operation)
        if previous:
            return self.recover_handle(previous, fingerprint)
        if not establish:
            return None
        now = self.c.clock()
        changed = self.store.execute(
            """UPDATE room_host_control_handles
            SET state='retiring',expires_at=MIN(expires_at,?),exchange_secret=''
            WHERE id=? AND host_connection_id=? AND host_revocation_epoch=? AND state='active' AND expires_at>?""",
            (
                now + HANDLE_REPLAY_WINDOW,
                route["control_handle_id"],
                host["id"],
                host["host_revocation_epoch"],
                now,
            ),
        )
        if changed.rowcount != 1:
            raise DevError(
                "ROOM_CONNECTION_CONTROL_CHANGED", "原连接控制凭据已变化，请使用当前凭据", 409
            )
        self.c.room_connections.touch_authority(host["id"])
        return self.create_handle(host, operation, fingerprint)

    def create_handle(self, host, operation, fingerprint):
        now = self.c.clock()
        identifier, token = "hc_" + uuid.uuid4().hex, secrets.token_urlsafe(32)
        handle = identifier + "." + token
        self.store.execute(
            """INSERT INTO room_host_control_handles
            (id,host_connection_id,host_revocation_epoch,secret_digest,purpose,state,expires_at,
             created_by_operation_id,created_at,exchange_digest,exchange_secret,exchange_expires_at)
            VALUES (?,?,?,?,'binding_control','active',?,?,?,?,?,?)""",
            (
                identifier,
                host["id"],
                host["host_revocation_epoch"],
                self.keyed_digest("handle", token),
                now + HANDLE_LIFETIME,
                operation,
                now,
                fingerprint,
                self.store.encrypt(handle),
                now + HANDLE_REPLAY_WINDOW,
            ),
        )
        return handle

    def control(self, raw, principal):
        args = validate(HostControl, raw)
        with self.store.transaction():
            self.c.guard()
            principal = refresh_principal(self.store, principal)
            self.c.notification_reader(principal)
            challenge = self.store.one(
                "SELECT * FROM room_host_attach_challenges WHERE owner_operation_id=?",
                (args["owner_operation_id"],),
            )
            if not challenge:
                routes = getattr(self.rooms, "routes", None)
                if routes is not None:
                    return routes.control(args, principal)
                raise DevError("ROOM_CONNECTION_CONTROL_NOT_FOUND", "没有可继续的连接控制操作", 404)
            host = self.current_host(challenge, principal)
            fingerprint = self.keyed_digest("attach-exchange", [principal.grant_id, args])
            replay = challenge["state"] == "consumed"
            if replay and not hmac.compare_digest(challenge["exchange_digest"], fingerprint):
                raise DevError("ROOM_CONNECTION_CONTROL_REDEEMED", "该连接证明已由原操作使用", 409)
            if args.get("control_handle"):
                self.authorize_handle(principal, host, args["control_handle"], replay=replay)
            elif not hmac.compare_digest(
                challenge["secret_digest"], self.keyed_digest("attach", args["challenge_proof"])
            ):
                raise DevError(
                    "ROOM_CONNECTION_IDENTITY_REQUIRED", "需要当前宿主的连接控制证明", 403
                )
            target, invitation = self.target(challenge)
            if not replay:
                if challenge["state"] != "pending" or challenge["expires_at"] <= self.c.clock():
                    raise DevError("ROOM_CONNECTION_CONTROL_EXPIRED", "连接控制证明已过期", 410)
                if args.get("challenge_proof") and not self.live_source(challenge, host):
                    raise DevError("ROOM_CONNECTION_CONTROL_EXPIRED", "原连接验证路线已变化", 409)
                if target["grant_id"] and target["grant_id"] != principal.grant_id:
                    raise DevError(
                        "ROOM_CONNECTION_IDENTITY_REQUIRED", "目标成员的接入身份已变化", 403
                    )
                table, identifier = self.rooms.binding_storage(target)
                now = self.c.clock()
                changed = self.store.execute(
                    f"""UPDATE {table}
                    SET host_control_evidence_id=?,host_control_verified_at=?,version=version+1,updated_at=?
                    WHERE id=? AND binding_generation=? AND host_control_evidence_id='' """,
                    (challenge["id"], now, now, identifier, challenge["target_generation"]),
                )
                if changed.rowcount != 1:
                    raise DevError("ROOM_BINDING_CHANGED", "目标成员的接入流程已变化", 409)
                self.store.execute(
                    """UPDATE room_connect_flows SET version=version+1,updated_at=?
                    WHERE member_id=?""",
                    (now, target["id"]),
                )
                self.store.execute(
                    """UPDATE room_host_attach_challenges SET state='consumed',exchange_digest=?,
                    consumed_by_operation_id=?,encrypted_secret='' WHERE id=?""",
                    (fingerprint, args["idempotency_key"], challenge["id"]),
                )
                self.rooms.change(
                    target["conversation_id"], "connection", target["id"], target["version"] + 1
                )
            return self.receipt(challenge, target, invitation)

    def live_source(self, challenge, host):
        return self.store.one(
            """SELECT 1 FROM room_members m
            JOIN conversation_rooms r ON r.id=m.conversation_id
            JOIN mcp_event_subscriptions s ON s.id=m.subscription_id
            WHERE m.subscription_id=? AND m.host_connection_id=? AND m.host_revocation_epoch=?
              AND m.status='bound' AND r.state='active' AND m.probe_replied_at IS NOT NULL
              AND m.subscription_verified_at IS NOT NULL AND s.state='active'
              AND (s.expires_at IS NULL OR s.expires_at>?)""",
            (
                challenge["source_subscription_id"],
                host["id"],
                host["host_revocation_epoch"],
                self.c.clock(),
            ),
        )

    def receipt(self, challenge, target, invitation):
        from shared.public_collaboration import checked_request

        result = {
            "owner_operation_id": challenge["owner_operation_id"],
            "member_id": target["id"],
            "binding_generation": target["binding_generation"],
            "state": "control_verified",
            "grants_authority": False,
        }
        if (
            not invitation["redeemed_at"]
            and invitation["invite_secret"]
            and invitation["expires_at"]
            and invitation["expires_at"] > self.c.clock()
        ):
            result["next_request"] = checked_request(
                "collaboration_room_connect_join",
                {
                    "invite_code": self.store.decrypt(invitation["invite_secret"]),
                    "idempotency_key": self.join_key(challenge["id"]),
                },
            )
        return result

    def join_key(self, challenge_id):
        return "host-join-" + self.keyed_digest("attach-join", challenge_id)

    def authorize_join(self, member, principal, args):
        if not member.get("host_connection_id"):
            return
        adoption = self.store.one(
            """SELECT evidence_kind FROM room_host_binding_adoptions
            WHERE member_id=? AND binding_generation=?""",
            (member["id"], member["binding_generation"]),
        )
        if adoption and adoption["evidence_kind"] != "verified_reuse":
            return  # The independent invite predates the host it established.
        self.require_preselection(member, principal)
        if not hmac.compare_digest(
            self.join_key(member["host_control_evidence_id"]), args["idempotency_key"]
        ):
            raise DevError("ROOM_CONNECTION_CONTROL_REQUIRED", "请在已验证的原 dot 中继续加入", 403)

    def require_preselection(self, member, principal):
        if not member["host_connection_id"]:
            return None
        reference = {
            "host_connection_id": member["host_connection_id"],
            "host_revocation_epoch": member["host_revocation_epoch"],
        }
        host = self.current_host(reference, principal)
        evidence = member["host_control_evidence_id"]
        proof = self.store.one(
            """SELECT 1 FROM room_host_attach_challenges WHERE id=?
            AND state='consumed' AND target_member_id=? AND target_generation=?
            AND host_connection_id=? AND host_revocation_epoch=?""",
            (
                evidence,
                member["id"],
                member["binding_generation"],
                host["id"],
                host["host_revocation_epoch"],
            ),
        )
        if member["host_control_verified_at"] is None or not proof:
            raise DevError(
                "ROOM_CONNECTION_CONTROL_REQUIRED", "等待原 dot 确认此房间的接入流程", 409
            )
        return host
