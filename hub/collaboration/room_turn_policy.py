"""Resolve the server's policy for newly claimed room consumer turns."""

from __future__ import annotations

import logging
import os

from hub.collaboration import telemetry
from hub.collaboration.common import validate
from shared.room_contracts import RoomTurnPolicyUpdate
from shared.util import DevError

POLICY_REVISION = 1
ROUTE_GRACE_SECONDS = 900
MAX_TURN_AGE_SECONDS = 3600
MAX_TURN_LEASE_SECONDS = 300
_MODES = frozenset({"strict", "shadow", "decoupled"})
_ACTIONS = frozenset(
    {
        "reply",
        "notice_ack",
        "work_start",
        "work_continue",
        "context_update",
        "handoff",
        "create",
        "assign",
        "claim",
        "heartbeat",
        "progress",
        "execute",
        "result",
        "archive_result",
        "publish",
        "read",
        "begin",
        "scope_update",
        "other",
    }
)
_REASONS = frozenset({"inbox_revision", "scope_changed", "route_expired", "source_changed"})
_LOG = logging.getLogger(__name__)


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_turn_policy_overrides (
        conversation_id TEXT PRIMARY KEY REFERENCES conversation_rooms(id),
        mode TEXT CHECK(mode IN ('strict','shadow','decoupled')),
        version INTEGER NOT NULL CHECK(version >= 1),
        updated REAL NOT NULL
    )""")


def _default():
    configured = os.environ.get("CODEPIER_ROOM_CHAT_SCOPE_MODE", "strict").strip().lower()
    return configured if configured in _MODES else "strict"


def view(store, conversation_id):
    saved = store.one(
        "SELECT mode,version FROM room_turn_policy_overrides WHERE conversation_id=?",
        (conversation_id,),
    )
    override = saved["mode"] if saved else None
    default = _default()
    return {
        "conversation_id": conversation_id,
        "mode": override or default,
        "override": override,
        "instance_default": default,
        "version": saved["version"] if saved else 0,
        "policy_revision": POLICY_REVISION,
        "applies_to": "new_turns",
        "max_turn_age_seconds": MAX_TURN_AGE_SECONDS,
        "max_lease_seconds": MAX_TURN_LEASE_SECONDS,
        "route_grace_seconds": ROUTE_GRACE_SECONDS,
        "grants_authority": False,
    }


def mode(store, conversation_id):
    return view(store, conversation_id)["mode"]


def _owner(collaboration, principal, conversation_id):
    actor, room = collaboration.rooms.owner(principal, conversation_id, include_archived=True)
    if not actor.instance_admin:
        raise DevError("INSTANCE_ADMIN_REQUIRED", "修改回合策略需要实例管理员权限", 403)
    return actor, room


def read(collaboration, conversation_id, principal):
    with collaboration.store.transaction(immediate=False):
        _owner(collaboration, principal, conversation_id)
        return view(collaboration.store, conversation_id)


def update(collaboration, raw, principal):
    args = validate(RoomTurnPolicyUpdate, raw)
    store = collaboration.store
    with store.transaction():
        actor, room = _owner(collaboration, principal, args["conversation_id"])
        collaboration.rooms.require_native(room)

        def perform():
            current = view(store, room["id"])
            if current["version"] != args["expected_version"]:
                raise DevError(
                    "ROOM_TURN_POLICY_CHANGED",
                    "回合策略已变化，请读取当前版本后再提交",
                    409,
                    current_version=current["version"],
                )
            version = current["version"] + 1
            store.execute(
                """INSERT INTO room_turn_policy_overrides(conversation_id,mode,version,updated)
                VALUES (?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET
                mode=excluded.mode, version=excluded.version, updated=excluded.updated""",
                (room["id"], args["mode"], version, collaboration.clock()),
            )
            result = view(store, room["id"])
            store.audit(
                actor.actor,
                "room.turn_policy",
                room["id"],
                "ok",
                {
                    "previous_mode": current["mode"],
                    "mode": result["mode"],
                    "previous_override": current["override"],
                    "override": result["override"],
                    "version": version,
                    "reason": args["reason"],
                },
            )
            return result

        return collaboration.rooms.mutation(actor, "room_turn_policy", args, perform)


def record_shadow(action, decision, reason):
    if (
        action not in _ACTIONS
        or decision not in {"would_allow", "would_block"}
        or reason not in _REASONS
    ):
        return
    try:
        stage = (
            telemetry.Stage.SHADOW_WOULD_ALLOW
            if decision == "would_allow"
            else telemetry.Stage.SHADOW_WOULD_BLOCK
        )
        telemetry.collaboration_telemetry.record(
            telemetry.Action(action),
            stage,
            channel=telemetry.Channel.ROOM,
            reason=telemetry.Reason(reason),
        )
        _LOG.info(
            "room_turn_shadow action=%s decision=%s reason=%s revision=%s",
            action,
            decision,
            reason,
            POLICY_REVISION,
            extra={
                "action_class": action,
                "decision_class": decision,
                "reason_code": reason,
                "policy_revision": POLICY_REVISION,
            },
        )
    except Exception:
        # A logging handler failure cannot change the authoritative decision.
        pass
