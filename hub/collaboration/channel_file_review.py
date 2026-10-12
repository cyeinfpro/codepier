"""Current, bounded Weixin file review; preview never uploads or grants sharing."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from hub.collaboration.common import digest
from hub.collaboration.weixin_schema import Mutation
from hub.collaboration.weixin_protocol import MAX_MEDIA, safe_filename
from shared.util import DevError


class Selection(Mutation):
    message_id: str = Field(min_length=1, max_length=100)
    expected_message_version: int = Field(ge=1)
    artifact_ids: list[str] = Field(min_length=1, max_length=16)


class Reviewed(Selection):
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm_file_egress: Literal[True]


class ChannelFileReview:
    def __init__(self, media):
        self.media, self.w, self.c, self.store = media, media.w, media.c, media.store

    def selection(self, raw, principal, model):
        args, principal, room = self.w.owner(raw, principal, model)
        message = self.c.object("collaboration_messages", room, args["message_id"])
        self.c.chatroom.require_message_visible(principal, message)
        if message["version"] != args["expected_message_version"] or message["conversation_id"] != args["conversation_id"]:
            raise DevError("STALE_VERSION", "原回复版本或话题已改变", 409)
        source = self.w.delivery.root_source(message)
        origin = json.loads(source["body"]).get("external_origin", {}) if source else {}
        binding, account, *_ = self.w.valid(origin.get("binding_id", ""))
        body = json.loads(message["body"])
        if (message["origin"] != "bound_connector" or body.get("sender_dot_id") != binding["dot_id"]
                or body.get("dot_reply_complete") is False or not binding["allow_media_outbound"]
                or not binding["allow_replies"] or origin.get("generation") != binding["generation"]
                or origin.get("peer_id") != binding["peer_id"]):
            raise DevError("ATTACHMENT_REPLY_REQUIRED", "需要原微信联系人、当前 dot 最终回复及文件外发方向批准", 403)
        if len(set(args["artifact_ids"])) != len(args["artifact_ids"]):
            raise DevError("ATTACHMENT_DUPLICATE", "文件清单不能重复", 422)
        artifacts = []
        for identifier in args["artifact_ids"]:
            artifact, _ = self.c.runtime.artifacts.row(identifier, principal)
            if (artifact["project_id"] != binding["project_id"] or artifact["space_id"] != binding["space_id"]
                    or artifact["expires"] <= self.c.clock() or artifact["bytes"] > MAX_MEDIA):
                raise DevError("ATTACHMENT_ARTIFACT_SCOPE", "只能选择当前项目未过期、预算内的实际产物", 403)
            artifacts.append(artifact)
        proof = digest([binding["id"], binding["version"], binding["generation"], account["credential_version"],
                        binding["peer_id"], binding["project_id"], binding["conversation_id"],
                        message["id"], message["version"],
                        [(a["id"], a["name"], a["sha256"], a["bytes"], a["expires"]) for a in artifacts]])
        return args, principal, binding, message, artifacts, proof

    def preview(self, raw, principal):
        with self.store.transaction(immediate=False):
            _, _, binding, message, artifacts, proof = self.selection(raw, principal, Selection)
            return {"preview_digest": proof, "binding_id": binding["id"], "binding_version": binding["version"],
                    "recipient": {"channel": "weixin", "peer_id": binding["peer_id"]},
                    "project_id": binding["project_id"], "conversation_id": binding["conversation_id"],
                    "message_id": message["id"], "message_version": message["version"],
                    "files": [{"artifact_id": a["id"], "name": safe_filename(a["name"]), "bytes": a["bytes"],
                               "sha256": a["sha256"], "expires_at": a["expires"],
                               "source_operation_id": a.get("source_operation_id") or None,
                               "type": "application/octet-stream", "type_verified": False} for a in artifacts],
                    "total_bytes": sum(a["bytes"] for a in artifacts), "upload_limit": MAX_MEDIA,
                    "queued": False, "automatic_publication": False, "read_only": True}

    def approve(self, raw, principal):
        with self.store.transaction():
            args, principal, _, _, _, proof = self.selection(raw, principal, Reviewed)
            if proof != args["preview_digest"]:
                raise DevError("ATTACHMENT_PREVIEW_STALE", "文件版本、原收件人或范围已变化，请重新预览并确认", 409)
            # Selection and the original sharing mutation share one transaction.
            # Do not widen scope or replace the source-reply delivery owner.
            return self.media.add_artifacts(
                {key: value for key, value in args.items() if key != "preview_digest"}, principal)
