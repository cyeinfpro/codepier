"""One canonical room history; mirrors are transport jobs, never model messages."""

import asyncio
import json
import logging
import uuid

from hub.collaboration import telegram_schema as models
from hub.collaboration.channel_binding import ChannelBinding
from hub.collaboration.common import canonical, digest
from hub.collaboration.telegram_protocol import split_text
from shared.util import DevError

logger = logging.getLogger(__name__)

MAX_DAILY_MIRRORS = 2000

FINAL_PARTS = {"accepted", "owner_confirmed", "cancelled", "rejected", "superseded"}


def message_digest(message):
    body = json.loads(message["body"])
    return digest(
        [
            message["id"],
            message["version"],
            message["room_id"],
            message["conversation_id"],
            message["reply_to_id"],
            message["origin"],
            body.get("body_text", ""),
            body.get("sender_dot_id"),
            body.get("dot_reply_complete", True),
            body.get("provenance_project_ids"),
            body.get("external_origin"),
        ]
    )


class ChannelRouter:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        self.bindings = ChannelBinding(collaboration)
        from hub.collaboration.channel_media_router import ChannelMediaRouter

        self.media = ChannelMediaRouter(self)
        from hub.collaboration.channel_capabilities import ChannelCapabilities

        self.capabilities = ChannelCapabilities(self)

    def consent_weixin(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.c.weixin.owner(
                raw, principal, models.MirrorConsent
            )
            row, *_ = self.bindings.validate("weixin", args["binding_id"])
            if (
                row["room_id"] != room["id"]
                or row["conversation_id"] != args["conversation_id"]
                or row["version"] != args["expected_version"]
            ):
                raise DevError("STALE_VERSION", "原微信绑定或话题已变化", 409)

            def save():
                self.store.execute(
                    """INSERT OR REPLACE INTO channel_mirror_consent VALUES ('weixin',?,?,?,?)""",
                    (row["id"], row["version"], row["generation"], self.c.clock()),
                )
                self.c.audit(
                    room,
                    principal.actor,
                    "weixin.full_mirror_approved",
                    row["id"],
                    {"historical_replay": False},
                )
                return {
                    "binding_id": row["id"],
                    "full_mirror": True,
                    "historical_replay": False,
                    "notification_note": "微信没有服务端静默字段，请在微信客户端设置免打扰。",
                }

            return self.c.mutation(principal, room, "weixin_mirror_consent", args, save)

    def wx_consent(self, binding):
        return self.store.one(
            """SELECT 1 FROM channel_mirror_consent WHERE channel='weixin' AND binding_id=?
            AND binding_version=? AND generation=?""",
            (binding["id"], binding["version"], binding["generation"]),
        )

    def queue_reply(self, message):
        return self.queue(message)

    def queue_reply_dispositions(self, message, *, delivery_dependency=None):
        dispositions = []
        self.queue(message, dispositions=dispositions, delivery_dependency=delivery_dependency)
        return dispositions[:24] or [{
            "channel": "weixin", "disposition": "not_requested", "reason_code": "no_external_reply"
        }]

    def queue(self, message, *, dispositions=None, delivery_dependency=None):
        self.store.require_transaction()
        body = json.loads(message["body"])
        if message["origin"] not in {
            "external_channel",
            "panel_owner",
            "bound_connector",
        }:
            return []
        # Legacy source reply remains byte-for-byte compatible until explicit
        # full-mirror consent is recorded for that exact binding version.
        source = self.c.weixin.delivery.root_source(message)
        origin = json.loads(source["body"]).get("external_origin", {}) if source else {}
        legacy = None
        if origin.get("channel") == "weixin":
            direct_reply = self.c.weixin.delivery.queue_reply_result(message, **(delivery_dependency or {}))
            legacy = direct_reply.delivery
            if dispositions is not None and direct_reply.disposition != "not_requested":
                dispositions.append(direct_reply.view())
        elif delivery_dependency:
            raise DevError('WEIXIN_DELIVERY_RELATION_INVALID', '当前回复没有可承接原依赖的微信收件人', 409)
        targets = []
        if self.c.config.enabled:
            targets += [
                ("telegram", r)
                for r in self.store.all(
                    """SELECT * FROM telegram_bindings
                WHERE room_id=? AND conversation_id=? AND state='active' AND mirror_enabled=1""",
                    (message["room_id"], message["conversation_id"]),
                )
            ]
        if self.c.config.enabled:
            targets += [
                ("weixin", r)
                for r in self.store.all(
                    """SELECT b.* FROM weixin_bindings b
                JOIN channel_mirror_consent s ON s.channel='weixin' AND s.binding_id=b.id AND s.binding_version=b.version
                AND s.generation=b.generation WHERE b.room_id=? AND b.conversation_id=? AND b.state='active'""",
                    (message["room_id"], message["conversation_id"]),
                )
            ]
        queued = []
        for channel, binding in targets:
            # Keep the original Weixin source-reply/file ledger as the single
            # transport owner; full mirroring must not add a duplicate text.
            if (channel == "weixin" and origin.get("channel") == "weixin"
                    and origin.get("binding_id") == binding["id"]
                    and direct_reply.disposition != "not_requested"):
                older = self.store.all(
                    """SELECT d.id FROM channel_mirrors d JOIN collaboration_messages m ON m.id=d.message_id
                    WHERE d.channel='weixin' AND d.binding_id=? AND m.reply_to_id=?
                    AND json_extract(m.body,'$.sender_dot_id')=? AND json_extract(m.body,'$.dot_reply_complete')=0""",
                    (binding["id"], message["reply_to_id"], body.get("sender_dot_id")),
                )
                for prior in older if legacy else ():
                    self.store.execute(
                        "UPDATE channel_mirror_parts SET state='superseded' WHERE delivery_id=? AND state IN ('queued','waiting_context','preparing')",
                        (prior["id"],),
                    )
                    self.refresh(prior["id"])
                continue
            direct = body.get("external_origin", {})
            if (
                direct.get("channel") == channel
                and direct.get("binding_id") == binding["id"]
            ):
                continue
            try:
                self.bindings.message(
                    channel,
                    binding["id"],
                    message,
                    version=binding["version"],
                    generation=binding["generation"],
                )
            except DevError as exc:
                if dispositions is not None:
                    dispositions.append({"channel": channel, "binding_id": binding["id"],
                                         "disposition": "blocked", "reason_code": exc.code})
                continue
            if body.get("sender_dot_id") and body["sender_dot_id"] != binding["dot_id"]:
                continue
            old = self.store.one(
                """SELECT * FROM channel_mirrors WHERE channel=? AND binding_id=? AND generation=?
                AND message_id=? AND message_version=?""",
                (
                    channel,
                    binding["id"],
                    binding["generation"],
                    message["id"],
                    message["version"],
                ),
            )
            if old:
                queued.append(old)
                if dispositions is not None:
                    dispositions.append({"channel": channel, "binding_id": binding["id"],
                                         "disposition": "existing", "reason_code": "", "delivery_id": old["id"]})
                continue
            limited = (
                self.store.one(
                    "SELECT COUNT(*) AS n FROM channel_mirrors WHERE binding_id=? AND created>?",
                    (binding["id"], self.c.clock() - 86400),
                )["n"]
                >= MAX_DAILY_MIRRORS
            )
            for prior in self.store.all(
                "SELECT id FROM channel_mirrors WHERE channel=? AND binding_id=? AND message_id=? AND message_version<?",
                (channel, binding["id"], message["id"], message["version"]),
            ):
                self.store.execute(
                    "UPDATE channel_mirror_parts SET state='superseded' WHERE delivery_id=? AND state IN ('queued','waiting_context','preparing')",
                    (prior["id"],),
                )
                self.refresh(prior["id"])
            did, now = uuid.uuid4().hex, self.c.clock()
            self.store.execute(
                """INSERT INTO channel_mirrors
                (id,channel,binding_id,binding_version,generation,credential_version,message_id,message_version,message_digest,
                room_id,conversation_id,state,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    did,
                    channel,
                    binding["id"],
                    binding["version"],
                    binding["generation"],
                    binding["credential_version"],
                    message["id"],
                    message["version"],
                    message_digest(message),
                    message["room_id"],
                    message["conversation_id"],
                    now,
                    now,
                ),
            )
            if limited:
                self.store.execute(
                    """INSERT INTO channel_mirror_parts
                    (id,delivery_id,ordinal,kind,body,state,reason_code,created,updated)
                    VALUES (?,?,0,'text','{}','rejected','daily_mirror_quota',?,?)""",
                    (uuid.uuid4().hex, did, now, now),
                )
                self.refresh(did)
                if dispositions is not None:
                    dispositions.append({"channel": channel, "binding_id": binding["id"],
                                         "disposition": "rejected", "reason_code": "daily_mirror_quota",
                                         "delivery_id": did})
                queued.append(
                    {"id": did, "channel": channel, "state": "needs_attention"}
                )
                continue
            incomplete = body.get("dot_reply_complete") is False
            # Coalesce unsent progress for the same original reply thread only.
            if body.get("sender_dot_id") and message["reply_to_id"]:
                older = self.store.all(
                    """SELECT d.id FROM channel_mirrors d JOIN collaboration_messages m ON m.id=d.message_id
                    WHERE d.binding_id=? AND d.channel=? AND d.id!=? AND d.state='queued' AND m.reply_to_id=?
                    AND json_extract(m.body,'$.sender_dot_id')=? AND json_extract(m.body,'$.dot_reply_complete')=0""",
                    (
                        binding["id"],
                        channel,
                        did,
                        message["reply_to_id"],
                        body["sender_dot_id"],
                    ),
                )
                for row in older:
                    if not self.store.one(
                        "SELECT 1 FROM channel_mirror_parts WHERE delivery_id=? AND state NOT IN ('queued','waiting_context')",
                        (row["id"],),
                    ):
                        self.store.execute(
                            "UPDATE channel_mirror_parts SET state='superseded' WHERE delivery_id=?",
                            (row["id"],),
                        )
                        self.store.execute(
                            "UPDATE channel_mirrors SET state='superseded' WHERE id=?",
                            (row["id"],),
                        )
            label = {"weixin": "微信", "telegram": "Telegram"}.get(
                body.get("external_origin", {}).get("channel"), "面板"
            )
            if body.get("sender_dot_id"):
                label = "dot · " + ("处理中" if incomplete else "最终回复")
            prefix = (
                "[" + label + " · 协作室 " + message["conversation_id"][-8:] + "]\n"
            )
            text = prefix + body.get("body_text", "")
            if message["reply_to_id"]:
                text += "\n↳ 原消息 " + message["reply_to_id"][-8:]
            for attachment in self.media.for_message(message["id"]):
                text += (
                    "\n["
                    + attachment["kind"]
                    + "] "
                    + attachment["name"]
                    + " · "
                    + attachment["state"]
                    + "（文件内容需单独批准分享）"
                )
            pieces = split_text(text, 3500 if channel == "telegram" else 700)
            for ordinal, piece in enumerate(pieces):
                if len(pieces) > 1:
                    piece = (
                        "(" + str(ordinal + 1) + "/" + str(len(pieces)) + ") " + piece
                    )
                self.store.execute(
                    """INSERT INTO channel_mirror_parts
                    (id,delivery_id,ordinal,kind,body,state,not_before,created,updated)
                    VALUES (?,?,?,'text',?,'queued',?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        did,
                        ordinal,
                        canonical({"text": piece}),
                        now + (3 if incomplete else 0),
                        now,
                        now,
                    ),
                )
            queued.append({"id": did, "channel": channel, "state": "queued"})
            if dispositions is not None:
                dispositions.append({"channel": channel, "binding_id": binding["id"],
                                     "disposition": "queued", "reason_code": "", "delivery_id": did})
        return queued or legacy

    def validate(self, delivery):
        message = self.store.one(
            "SELECT * FROM collaboration_messages WHERE id=?", (delivery["message_id"],)
        )
        if not message or message_digest(message) != delivery["message_digest"]:
            raise DevError("CHANNEL_SOURCE_CHANGED", "原镜像正文或来源版本已改变", 403)
        result = self.bindings.message(
            delivery["channel"],
            delivery["binding_id"],
            message,
            version=delivery["binding_version"],
            generation=delivery["generation"],
        )
        binding, account, *_ = result
        if account["credential_version"] != delivery["credential_version"]:
            raise DevError("CHANNEL_CREDENTIAL_CHANGED", "原镜像凭据版本已变化", 403)
        if delivery["channel"] == "weixin" and not self.wx_consent(binding):
            raise DevError("CHANNEL_MIRROR_REVOKED", "原完整同步批准已失效", 403)
        if delivery["channel"] == "telegram" and not binding["mirror_enabled"]:
            raise DevError("CHANNEL_MIRROR_REVOKED", "原完整同步批准已失效", 403)
        return (*result, message)

    def refresh(self, did):
        states = {
            r["state"]
            for r in self.store.all(
                "SELECT state FROM channel_mirror_parts WHERE delivery_id=?", (did,)
            )
        }
        state = (
            "needs_attention"
            if states & {"unknown", "rejected"}
            else "accepted"
            if states and states <= {"accepted", "owner_confirmed"}
            else "superseded"
            if states == {"superseded"}
            else "cancelled"
            if states and states <= FINAL_PARTS
            else "sending"
            if "sending" in states
            else "waiting_context"
            if "waiting_context" in states
            else "queued"
        )
        self.store.execute(
            "UPDATE channel_mirrors SET state=?,updated=? WHERE id=? AND state!=?",
            (state, self.c.clock(), did, state),
        )

    def quarantine(self, channel, bid, reason):
        rows = self.store.all(
            """SELECT d.id FROM channel_mirrors d WHERE d.channel=? AND d.binding_id=?
            AND EXISTS (SELECT 1 FROM channel_mirror_parts p WHERE p.delivery_id=d.id
                AND p.state IN ('queued','waiting_context','preparing'))""",
            (channel, bid),
        )
        for row in rows:
            self.store.execute(
                """UPDATE channel_mirror_parts SET state='rejected',reason_code=?
                WHERE delivery_id=? AND state IN ('queued','waiting_context','preparing')""",
                (reason, row["id"]),
            )
            self.refresh(row["id"])

    def reconcile(self):
        with self.store.transaction():
            expired = self.store.all(
                "SELECT DISTINCT delivery_id FROM channel_mirror_parts WHERE state='sending' AND lease_until<=?",
                (self.c.clock(),),
            )
            self.store.execute(
                """UPDATE channel_mirror_parts SET state='unknown',reason_code='send_interrupted'
                WHERE state='sending' AND lease_until<=?""",
                (self.c.clock(),),
            )
            self.store.execute(
                """UPDATE channel_mirror_parts SET state='queued',lease_fence=lease_fence+1
                WHERE state='preparing' AND lease_until<=?""",
                (self.c.clock(),),
            )
            for row in expired:
                self.refresh(row["delivery_id"])

    def claim(self, pid):
        with self.store.transaction():
            row = self.store.one(
                "SELECT * FROM channel_mirror_parts WHERE id=?", (pid,)
            )
            now = self.c.clock()
            if (
                not row
                or row["state"] not in {"queued", "waiting_context"}
                or row["not_before"] > now
            ):
                return None
            delivery = self.store.one(
                "SELECT * FROM channel_mirrors WHERE id=?", (row["delivery_id"],)
            )
            if self.store.one(
                "SELECT 1 FROM channel_mirror_parts WHERE delivery_id=? AND ordinal<? AND state NOT IN ('accepted','owner_confirmed')",
                (delivery["id"], row["ordinal"]),
            ):
                return None
            try:
                binding, account, owner, room, dot, message = self.validate(delivery)
            except DevError as exc:
                self.store.execute(
                    "UPDATE channel_mirror_parts SET state='rejected',reason_code=? WHERE id=?",
                    (exc.code, pid),
                )
                self.refresh(delivery["id"])
                return None
            if row["kind"] == "attachment":
                aid = json.loads(row["body"])["attachment_id"]
                try:
                    media = self.media.row(aid, owner)
                    if (
                        media["binding_id"] != binding["id"]
                        or media["direction"] != "outbound"
                    ):
                        raise DevError(
                            "ATTACHMENT_EGRESS_NOT_APPROVED", "附件目标不匹配", 403
                        )
                except DevError as exc:
                    self.store.execute(
                        "UPDATE channel_mirror_parts SET state='rejected',reason_code=? WHERE id=?",
                        (exc.code, pid),
                    )
                    self.refresh(delivery["id"])
                    return None
                if media["state"] in {"pending", "fetching"}:
                    self.store.execute(
                        "UPDATE channel_mirror_parts SET not_before=? WHERE id=?",
                        (now + 3, pid),
                    )
                    return None
                if media["state"] != "ready":
                    self.store.execute(
                        "UPDATE channel_mirror_parts SET state='rejected',reason_code=? WHERE id=?",
                        (media["reason_code"] or "attachment_unavailable", pid),
                    )
                    self.refresh(delivery["id"])
                    return None
            if delivery["channel"] == "telegram" and account["next_send"] > now:
                self.store.execute(
                    "UPDATE channel_mirror_parts SET not_before=? WHERE id=?",
                    (account["next_send"], pid),
                )
                return None
            context = ""
            if delivery["channel"] == "weixin":
                context = self.store.one(
                    "SELECT token FROM weixin_contexts WHERE account_id=? AND generation=? AND peer_id=?",
                    (account["id"], binding["generation"], binding["peer_id"]),
                )
                if not context:
                    self.store.execute(
                        "UPDATE channel_mirror_parts SET state='waiting_context',not_before=? WHERE id=?",
                        (now + 5, pid),
                    )
                    self.refresh(delivery["id"])
                    return None
                context = self.store.decrypt(context["token"])
            fence = row["lease_fence"] + 1
            self.store.execute(
                "UPDATE channel_mirror_parts SET state='preparing',lease_fence=?,lease_until=?,updated=? WHERE id=?",
                (fence, now + 60, now, pid),
            )
            return {
                **row,
                "lease_fence": fence,
                "delivery": delivery,
                "binding": binding,
                "account": account,
                "context": context,
                "message": message,
            }

    def begin_send(self, claim):
        with self.store.transaction():
            row = self.store.one(
                "SELECT * FROM channel_mirror_parts WHERE id=?", (claim["id"],)
            )
            if (
                not row
                or row["state"] != "preparing"
                or row["lease_fence"] != claim["lease_fence"]
                or row["lease_until"] <= self.c.clock()
            ):
                raise DevError("CHANNEL_SEND_FENCE_LOST", "原发送准备世代已失效", 409)
            _, account, *_ = self.validate(claim["delivery"])
            if claim["delivery"]["channel"] == "telegram":
                if account["next_send"] > self.c.clock():
                    self.store.execute(
                        "UPDATE channel_mirror_parts SET state='queued',not_before=?,lease_until=0 WHERE id=?",
                        (account["next_send"], row["id"]),
                    )
                    return None
                # Conservative one-per-bot pacing also respects per-chat guidance.
                self.store.execute(
                    "UPDATE telegram_accounts SET next_send=? WHERE id=?",
                    (self.c.clock() + 1.1, account["id"]),
                )
            self.store.execute(
                "UPDATE channel_mirror_parts SET state='sending',lease_until=?,updated=? WHERE id=?",
                (self.c.clock() + (960 if row["kind"] == "attachment" else 60), self.c.clock(), row["id"]),
            )
            self.refresh(row["delivery_id"])
            reference = self.store.one(
                """SELECT external_id FROM channel_message_map WHERE channel=? AND binding_id=?
                AND generation=? AND message_id=? ORDER BY external_id LIMIT 1""",
                (
                    claim["delivery"]["channel"],
                    claim["binding"]["id"],
                    claim["binding"]["generation"],
                    claim["message"]["reply_to_id"],
                ),
            )
            return reference["external_id"].split(":", 1)[-1] if reference else ""

    def finish(self, claim, external_id="", error=None):
        with self.store.transaction():
            row = self.store.one(
                "SELECT * FROM channel_mirror_parts WHERE id=?", (claim["id"],)
            )
            if (
                not row
                or row["state"] != "sending"
                or row["lease_fence"] != claim["lease_fence"]
            ):
                return
            state, reason, delay = "accepted", "", 0
            if error:
                reason = (
                    error.code if isinstance(error, DevError) else "transport_unknown"
                )
                if isinstance(error, DevError) and error.status == 429:
                    state, delay = (
                        "queued",
                        max(1, error.details.get("retry_after_seconds", 60)),
                    )
                    if claim["delivery"]["channel"] == "telegram":
                        self.store.execute(
                            "UPDATE telegram_accounts SET next_send=MAX(next_send,?) WHERE id=?",
                            (self.c.clock() + delay, claim["account"]["id"]),
                        )
                elif (
                    isinstance(error, DevError)
                    and error.code == "TELEGRAM_PROVIDER_REJECTED"
                ):
                    state = "rejected"
                else:
                    state = "unknown"
            self.store.execute(
                "UPDATE channel_mirror_parts SET state=?,external_id=?,reason_code=?,not_before=?,lease_until=0,updated=? WHERE id=?",
                (
                    state,
                    external_id,
                    reason,
                    self.c.clock() + delay,
                    self.c.clock(),
                    row["id"],
                ),
            )
            if state == "accepted" and external_id:
                self.store.execute(
                    "INSERT OR REPLACE INTO channel_message_map VALUES (?,?,?,?,?)",
                    (
                        claim["delivery"]["channel"],
                        claim["binding"]["id"],
                        claim["binding"]["generation"],
                        "out:" + external_id,
                        claim["message"]["id"],
                    ),
                )
            self.refresh(row["delivery_id"])

    def safe_claim(self, pid):
        try:
            return self.claim(pid)
        except Exception as exc:
            # claim is transactional, so failures before sending may be rejected
            # without inventing a network outcome or touching another live lease.
            with self.store.transaction():
                row = self.store.one("SELECT * FROM channel_mirror_parts WHERE id=?", (pid,))
                if row and row["state"] in {"queued", "waiting_context"}:
                    self.store.execute("UPDATE channel_mirror_parts SET state='rejected',reason_code=?,updated=? WHERE id=?",
                        (exc.code if isinstance(exc, DevError) else "claim_internal_error", self.c.clock(), pid))
                    self.refresh(row["delivery_id"])
            return None

    async def send_once(self, pid):
        claim = await self.store.run(self.safe_claim, pid)
        if not claim:
            return
        content = attachment = None
        try:
            if claim["kind"] == "attachment":
                content, attachment = await self.store.run(
                    self.media.outbound_content,
                    json.loads(claim["body"])["attachment_id"],
                    claim["binding"]["id"],
                )
            reference = await self.store.run(self.begin_send, claim)
            if reference is None:
                return
        except Exception as exc:

            def reject(exc=exc):
                self.store.execute(
                    "UPDATE channel_mirror_parts SET state='rejected',reason_code=? WHERE id=? AND state='preparing' AND lease_fence=?",
                    (exc.code if isinstance(exc, DevError) else "preparation_internal_error", claim["id"], claim["lease_fence"]),
                )
                self.refresh(claim["delivery"]["id"])

            await self.store.run(reject)
            return
        try:
            await self.store.run(self.c.channel_settings.require_enabled,
                                 claim["delivery"]["channel"], claim["account"])
            value = json.loads(claim["body"]).get("text", "")
            if claim["delivery"]["channel"] == "telegram":
                client = self.c.telegram.client_factory(
                    self.store.decrypt(claim["account"]["secret"])
                )
                if claim["kind"] == "attachment":
                    external_id = await client.send_document(
                        claim["binding"]["chat_id"],
                        content,
                        attachment["name"],
                        silent=claim["binding"]["disable_notification"],
                        reply_to=reference,
                    )
                else:
                    external_id = await client.send(
                        claim["binding"]["chat_id"],
                        value,
                        silent=claim["binding"]["disable_notification"],
                        reply_to=reference,
                    )
            else:
                client = self.c.weixin.client_factory(
                    claim["account"]["api_base"],
                    self.store.decrypt(claim["account"]["secret"]),
                )
                await client.send(
                    claim["binding"]["peer_id"],
                    claim["context"],
                    "cpm-" + claim["id"],
                    [{"type": 1, "text_item": {"text": value}}],
                )
                external_id = ""
            await self.store.run(self.finish, claim, external_id)
        except asyncio.CancelledError:
            await self.store.run(
                self.finish,
                claim,
                "",
                DevError("CHANNEL_SEND_CANCELLED", "提交期间停止，结果未知", 503),
            )
            raise
        except Exception as exc:
            await self.store.run(self.finish, claim, "", exc)

    async def tick(self):
        await self.c.channel_enrollment.notices.tick()
        await self.store.run(self.reconcile)
        rows = await self.store.run(
            lambda: self.store.all(
                """SELECT id FROM (
            SELECT p.id,p.updated,ROW_NUMBER() OVER (PARTITION BY d.channel,d.binding_id ORDER BY p.updated,p.id) AS lane
            FROM channel_mirror_parts p JOIN channel_mirrors d ON d.id=p.delivery_id
            WHERE p.state IN ('queued','waiting_context') AND p.not_before<=?
            AND NOT EXISTS (SELECT 1 FROM channel_mirror_parts earlier WHERE earlier.delivery_id=p.delivery_id
                AND earlier.ordinal<p.ordinal AND earlier.state NOT IN ('accepted','owner_confirmed')))
            WHERE lane=1 ORDER BY updated,id LIMIT 16""",
                (self.c.clock(),),
            )
        )
        semaphore = asyncio.Semaphore(4)

        async def send(row):
            async with semaphore:
                await self.send_once(row["id"])

        results = await asyncio.gather(*(send(row) for row in rows), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Channel mirror worker failed: %s", type(result).__name__)

    def listing(self, room_id, conversation_id):
        rows = self.store.all(
            """SELECT id,channel,binding_id,message_id,state,created,updated FROM channel_mirrors
            WHERE room_id=? AND conversation_id=? ORDER BY created DESC LIMIT 60""",
            (room_id, conversation_id),
        )
        for row in rows:
            row["parts"] = self.store.all(
                "SELECT ordinal,kind,state,reason_code FROM channel_mirror_parts WHERE delivery_id=? ORDER BY ordinal",
                (row["id"],),
            )
            row["delivered_verified"] = row["read_verified"] = False
        return rows

    def for_messages(self, ids):
        ids = list(dict.fromkeys(ids))[:124]
        result = self.c.weixin.delivery.for_messages(ids)
        for values in result.values():
            for value in values:
                value["channel"] = "weixin"
        if not ids:
            return result
        rows = self.store.all(
            "SELECT id,channel,binding_id,message_id,state FROM channel_mirrors WHERE message_id IN ("
            + ",".join("?" for _ in ids)
            + ")",
            tuple(ids),
        )
        parts = {}
        if rows:
            for part in self.store.all(
                "SELECT delivery_id,ordinal,kind,state,reason_code FROM channel_mirror_parts WHERE delivery_id IN ("
                + ",".join("?" for _ in rows) + ") ORDER BY delivery_id,ordinal",
                tuple(row["id"] for row in rows),
            ):
                delivery_id = part.pop("delivery_id")
                parts.setdefault(delivery_id, []).append(part)
        for row in rows:
            row["parts"] = parts.get(row["id"], [])
            result.setdefault(row["message_id"], []).append(row)
        for message_id, recorded in self.reply_dispositions(ids).items():
            active = {(item["channel"], item.get("binding_id")) for item in result.get(message_id, [])
                      if item.get("kind") != "status"}
            for disposition in recorded:
                if disposition["disposition"] not in {"blocked", "rejected"}:
                    continue
                if (disposition["channel"], disposition.get("binding_id")) in active:
                    continue
                # A blocked intent without an outbox has no control target.
                result.setdefault(message_id, []).append({
                    **disposition, "state": disposition["disposition"], "parts": [],
                    "delivered_verified": False, "read_verified": False,
                })
            if not recorded and message_id not in result:
                result[message_id] = [{
                    "channel": "weixin", "state": "insufficient_evidence", "parts": [],
                    "reason_code": "legacy_disposition_unavailable",
                }]
        return result

    def reply_dispositions(self, ids):
        # Only accept message IDs already filtered for visibility by the caller.
        # The correlated lookup uses the owner/conversation/message index.
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.store.all(f"""SELECT m.id, (
            SELECT r.result FROM room_runtime_receipts r
            WHERE r.owner_user_id=c.owner_user_id AND r.conversation_id=c.id
                AND json_valid(r.result) AND json_extract(r.result,'$.message_id')=m.id
                AND r.action LIKE 'room_reply:%'
                AND json_type(r.result,'$.channel_dispositions')='array'
            ORDER BY r.created,r.request_key LIMIT 1
            ) AS receipt FROM collaboration_messages m
            JOIN conversation_rooms c ON c.id=m.conversation_id
            WHERE m.id IN ({marks}) AND m.kind='reply' AND m.source_id LIKE 'room-reply:%'""",
            tuple(ids),
        )
        return {row["id"]: json.loads(row["receipt"])["channel_dispositions"] if row["receipt"] else []
                for row in rows}

    def control(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.c.telegram.owner(
                raw, principal, models.DeliveryControl, enabled=False
            )
            row = self.store.one(
                "SELECT * FROM channel_mirrors WHERE id=? AND room_id=? AND conversation_id=?",
                (args["delivery_id"], room["id"], args["conversation_id"]),
            )
            if not row:
                raise DevError("CHANNEL_DELIVERY_NOT_FOUND", "当前房间没有此投递", 404)

            def save():
                if args["action"] == "confirm_received":
                    if not args["confirmation_note"].strip():
                        raise DevError(
                            "CHANNEL_CONFIRMATION_REQUIRED", "须填写人工核对依据", 422
                        )
                    self.store.execute(
                        "UPDATE channel_mirror_parts SET state='owner_confirmed' WHERE delivery_id=? AND state='unknown'",
                        (row["id"],),
                    )
                else:
                    self.store.execute(
                        "UPDATE channel_mirror_parts SET state='cancelled' WHERE delivery_id=? AND state IN ('queued','waiting_context','preparing')",
                        (row["id"],),
                    )
                self.refresh(row["id"])
                self.c.audit(
                    room,
                    principal.actor,
                    "channel.delivery_" + args["action"],
                    row["id"],
                    {"note": args["confirmation_note"]},
                )
                return {"delivery_id": row["id"], "automatic_resend": False}

            return self.c.mutation(
                principal, room, "channel_delivery_control", args, save
            )
