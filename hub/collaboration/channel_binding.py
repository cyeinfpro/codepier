"""Revalidate one canonical channel binding against current owner and grant."""

import json
from hub.principal import Principal, refresh_principal
from hub.room_scope import snapshot
from shared.util import DevError


class ChannelBinding:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def validate(self, channel, identifier, *, pairing=False):
        if channel == "weixin":
            return self.c.weixin.valid(identifier, pairing=pairing)
        if channel != "telegram":
            raise DevError("CHANNEL_DISABLED", "通道不受支持", 409)
        row = self.store.one(
            "SELECT * FROM telegram_bindings WHERE id=?", (identifier,)
        )
        if row:
            self.c.telegram.require_enabled(row)
        states = {"active", "pairing", "awaiting_owner"} if pairing else {"active"}
        if not row or row["state"] not in states or row["expires_at"] <= self.c.clock():
            raise DevError(
                "CHANNEL_BINDING_INACTIVE", "原通道绑定已暂停、撤销或到期", 403
            )
        value = json.loads(row["owner_principal"])
        value["scopes"] = set(value["scopes"])
        owner = refresh_principal(self.store, Principal(**value))
        self.c.owner(owner)
        account = self.store.one(
            "SELECT * FROM telegram_accounts WHERE id=? AND space_id=? AND owner_user_id=?",
            (row["account_id"], owner.space_id, owner.user_id),
        )
        if (
            not account
            or account["state"] != "active"
            or row["credential_version"] != account["credential_version"]
            or row["generation"] != account["generation"]
        ):
            raise DevError("CHANNEL_CREDENTIAL_CHANGED", "原通道凭据或世代已变化", 403)
        _, room = self.c.scope(
            owner,
            {
                "project": row["project_id"],
                "environment_id": row["environment_id"],
                "conversation_id": row["conversation_id"],
            },
        )
        self.c.live_room(room)
        self.c.conversations.object(owner, row["conversation_id"])
        project = self.c.runtime.project(row["project_id"], owner)
        if (
            room["id"] != row["room_id"]
            or room["owner_user_id"] != row["owner_user_id"]
            or snapshot(project) != json.loads(row["project_snapshot"])
        ):
            raise DevError(
                "CHANNEL_MAPPING_CHANGED", "原房间项目、节点或目录变化，需重新确认", 403
            )
        dot = self.c.dots.find(row["dot_id"])
        if (
            not dot
            or dot["room_id"] != room["id"]
            or dot["conversation_id"] != row["conversation_id"]
        ):
            raise DevError("CHANNEL_DOT_CHANGED", "原话题 dot 已变化", 403)
        slot = self.c.dot_chat.authorize(owner, room, dot)
        self.c.grant_reader(
            room, slot["grant_id"], snapshot=json.loads(row["dot_principal"])
        )
        return row, account, owner, room, dot

    def message(self, channel, identifier, message, *, version=None, generation=None):
        binding, account, owner, room, dot = self.validate(channel, identifier)
        if (
            binding["room_id"] != message["room_id"]
            or binding["conversation_id"] != message["conversation_id"]
            or version is not None
            and binding["version"] != version
            or generation is not None
            and binding["generation"] != generation
        ):
            raise DevError(
                "CHANNEL_MESSAGE_SCOPE", "消息不属于当前原绑定、版本或世代", 403
            )
        self.c.chatroom.require_message_visible(owner, message)
        reader = self.c.grant_reader(
            room,
            self.c.dot_chat.authorize(owner, room, dot)["grant_id"],
            snapshot=json.loads(binding["dot_principal"]),
        )
        self.c.chatroom.require_message_visible(reader, message)
        # A newly added project in a conversation is not automatically egress-approved.
        projects = json.loads(message["body"]).get(
            "provenance_project_ids", [room["project_id"]]
        )
        if any(p != binding["project_id"] for p in projects):
            raise DevError(
                "CHANNEL_PROJECT_NOT_APPROVED",
                "消息包含原绑定未确认的项目，不能跨端外发",
                403,
            )
        return binding, account, owner, room, dot
