"""Exact owner-selected artifact egress to one confirmed Telegram identity."""

import json
import mimetypes
import uuid
from hub.collaboration import telegram_schema as models
from hub.collaboration.common import canonical, digest
from hub.collaboration.telegram_protocol import UPLOAD_LIMIT
from hub.collaboration.weixin_protocol import safe_filename
from shared.util import DevError


class TelegramFiles:
    def __init__(self, telegram):
        self.t, self.c, self.store = telegram, telegram.c, telegram.store

    def selection(self, raw, principal, model):
        args, principal, room = self.t.owner(raw, principal, model)
        binding, account, owner, _, dot = self.t.valid(args["binding_id"])
        if (
            binding["room_id"] != room["id"]
            or binding["conversation_id"] != args["conversation_id"]
            or binding["version"] != args["expected_binding_version"]
        ):
            raise DevError("STALE_VERSION", "原Telegram收件人、话题或绑定版本改变", 409)
        message = self.c.object("collaboration_messages", room, args["message_id"])
        self.c.channels.bindings.message(
            "telegram", binding["id"], message, version=binding["version"]
        )
        body = json.loads(message["body"])
        if (
            message["version"] != args["expected_message_version"]
            or message["origin"] != "bound_connector"
            or body.get("sender_dot_id") != dot["id"]
            or body.get("dot_reply_complete") is False
        ):
            raise DevError(
                "TELEGRAM_FILE_REPLY_SCOPE",
                "只能附加到当前dot在此协作室的最终回复",
                403,
            )
        if len(set(args["artifact_ids"])) != len(args["artifact_ids"]):
            raise DevError("ATTACHMENT_DUPLICATE", "请选择不重复的完整文件清单", 422)
        artifacts = []
        for aid in args["artifact_ids"]:
            artifact, _ = self.c.runtime.artifacts.row(aid, principal)
            if (
                artifact["project_id"] != binding["project_id"]
                or artifact["space_id"] != binding["space_id"]
                or artifact["expires"] <= self.c.clock()
                or artifact["bytes"] > UPLOAD_LIMIT
            ):
                raise DevError(
                    "TELEGRAM_FILE_SCOPE",
                    "只能选择此项目未过期且不超过50MB的产物；超限请在面板读取",
                    403,
                )
            artifacts.append(artifact)
        proof = digest(
            [
                binding["id"],
                binding["version"],
                binding["generation"],
                account["credential_version"],
                binding["user_id"],
                binding["chat_id"],
                message["id"],
                message["version"],
                [
                    (a["id"], a["name"], a["sha256"], a["bytes"], a["expires"])
                    for a in artifacts
                ],
            ]
        )
        return args, principal, room, binding, message, artifacts, proof

    def preview(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, principal, room, binding, message, artifacts, proof = self.selection(
                raw, principal, models.FileSelection
            )
            return {
                "preview_digest": proof,
                "binding_id": binding["id"],
                "binding_version": binding["version"],
                "recipient": {
                    "channel": "telegram",
                    "user_id": binding["user_id"],
                    "chat_id": binding["chat_id"],
                },
                "message_id": message["id"],
                "message_version": message["version"],
                "files": [
                    {
                        "artifact_id": a["id"],
                        "name": safe_filename(a["name"]),
                        "bytes": a["bytes"],
                        "sha256": a["sha256"],
                    }
                    for a in artifacts
                ],
                "total_bytes": sum(a["bytes"] for a in artifacts),
                "upload_limit": UPLOAD_LIMIT,
                "queued": False,
                "automatic_publication": False,
            }

    def approve(self, raw, principal):
        with self.store.transaction():
            args, principal, room, binding, message, artifacts, proof = self.selection(
                raw, principal, models.FileApprove
            )
            if args["preview_digest"] != proof:
                raise DevError(
                    "TELEGRAM_FILE_PREVIEW_STALE",
                    "文件或原收件人已改变，请重新预览后确认",
                    409,
                )

            def save():
                count = self.store.one(
                    """SELECT COUNT(*) AS n FROM channel_attachments
                    WHERE message_id=? AND binding_id=? AND direction='outbound'""",
                    (message["id"], binding["id"]),
                )["n"]
                if count + len(artifacts) > 16:
                    raise DevError(
                        "ATTACHMENT_COUNT_LIMIT", "每个原回复目标最多16件附件", 409
                    )
                self.c.channels.queue(message)
                delivery = self.store.one(
                    """SELECT * FROM channel_mirrors WHERE channel='telegram' AND binding_id=?
                    AND generation=? AND message_id=? AND message_version=?""",
                    (
                        binding["id"],
                        binding["generation"],
                        message["id"],
                        message["version"],
                    ),
                )
                if not delivery:
                    raise DevError(
                        "TELEGRAM_FILE_DELIVERY_REQUIRED",
                        "原绑定没有可用的正文投递记录",
                        409,
                    )
                self.c.channels.validate(delivery)
                if delivery["state"] in {"cancelled", "superseded"}:
                    raise DevError(
                        "TELEGRAM_FILE_DELIVERY_CLOSED", "原回复投递已取消或被替代", 409
                    )
                ordinal = self.store.one(
                    "SELECT COALESCE(MAX(ordinal),-1)+1 AS n FROM channel_attachments WHERE message_id=? AND direction='outbound'",
                    (message["id"],),
                )["n"]
                part = self.store.one(
                    "SELECT COALESCE(MAX(ordinal),-1)+1 AS n FROM channel_mirror_parts WHERE delivery_id=?",
                    (delivery["id"],),
                )["n"]
                ids = []
                now = self.c.clock()
                for index, artifact in enumerate(artifacts):
                    if self.store.one(
                        """SELECT 1 FROM channel_attachments WHERE message_id=? AND binding_id=?
                        AND direction='outbound' AND artifact_id=?""",
                        (message["id"], binding["id"], artifact["id"]),
                    ):
                        raise DevError(
                            "ATTACHMENT_ALREADY_APPROVED",
                            "此原回复已批准该产物，不重复外发",
                            409,
                        )
                    aid = uuid.uuid4().hex
                    name = safe_filename(artifact["name"])
                    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    kind = "image" if mime.startswith("image/") else "file"
                    self.store.execute(
                        """INSERT INTO channel_attachments
                        (id,space_id,owner_user_id,room_id,conversation_id,project_id,message_id,ordinal,direction,
                         binding_id,binding_version,generation,peer_id,kind,name,mime,size,sha256,artifact_id,state,
                         private_source,expires_at,created,updated,channel)
                        VALUES (?,?,?,?,?,?,?,?,'outbound',?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,'telegram')""",
                        (
                            aid,
                            binding["space_id"],
                            binding["owner_user_id"],
                            room["id"],
                            binding["conversation_id"],
                            binding["project_id"],
                            message["id"],
                            ordinal + index,
                            binding["id"],
                            binding["version"],
                            binding["generation"],
                            binding["chat_id"],
                            kind,
                            name,
                            mime,
                            artifact["bytes"],
                            artifact["sha256"],
                            artifact["id"],
                            self.store.encrypt(
                                canonical(
                                    {
                                        "artifact_id": artifact["id"],
                                        "approval_digest": proof,
                                    }
                                )
                            ),
                            min(binding["expires_at"], artifact["expires"]),
                            now,
                            now,
                        ),
                    )
                    self.store.execute(
                        """INSERT INTO channel_mirror_parts
                        (id,delivery_id,ordinal,kind,body,state,created,updated)
                        VALUES (?,?,?,'attachment',?,'queued',?,?)""",
                        (
                            uuid.uuid4().hex,
                            delivery["id"],
                            part + index,
                            canonical({"attachment_id": aid}),
                            now,
                            now,
                        ),
                    )
                    ids.append(aid)
                self.c.channels.refresh(delivery["id"])
                self.c.audit(
                    room,
                    principal.actor,
                    "telegram.files_egress_approved",
                    message["id"],
                    {
                        "binding_id": binding["id"],
                        "binding_version": binding["version"],
                        "attachment_ids": ids,
                        "preview_digest": proof,
                    },
                )
                return {
                    "attachment_ids": ids,
                    "delivery_id": delivery["id"],
                    "queued": True,
                    "delivered_verified": False,
                }

            return self.c.mutation(principal, room, "telegram_files", args, save)

    def candidates(self, principal, room, conversation_id):
        artifacts = self.c.runtime.artifacts.list(
            {"project": room["project_id"], "limit": 50, "cursor": ""}, principal
        )
        replies = []
        for row in self.store.all(
            """SELECT * FROM collaboration_messages WHERE room_id=? AND conversation_id=?
            AND origin='bound_connector' ORDER BY created DESC,id DESC LIMIT 50""",
            (room["id"], conversation_id),
        ):
            if not self.c.chatroom.message_visible(principal, row):
                continue
            body = json.loads(row["body"])
            if (
                body.get("sender_dot_id")
                and body.get("dot_reply_complete") is not False
            ):
                replies.append(
                    {
                        "id": row["id"],
                        "version": row["version"],
                        "body_text": body.get("body_text", "")[:120],
                    }
                )
        return {
            "artifact_candidates": [
                r for r in artifacts["artifacts"] if not r["expired"]
            ],
            "artifact_candidates_more": bool(artifacts["next_cursor"]),
            "reply_candidates": replies[:20],
            "upload_limit": UPLOAD_LIMIT,
        }
