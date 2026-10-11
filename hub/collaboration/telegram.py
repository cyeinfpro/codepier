"""Panel-confirmed private Telegram accounts. External chat never grants execution."""

import asyncio
import hashlib
import hmac
import json
import secrets
import uuid

from hub.collaboration import telegram_schema as models
from hub.collaboration.common import canonical, digest, redact, validate
from hub.collaboration.telegram_protocol import (
    TelegramAdapter,
    attachment_manifest,
    identifier,
)
from hub.collaboration.weixin import principal_record
from hub.room_scope import snapshot
from shared.util import DevError


class TelegramService:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        self.client_factory = TelegramAdapter
        from hub.collaboration.telegram_files import TelegramFiles

        self.files = TelegramFiles(self)

    def require_enabled(self):
        if not self.c.config.telegram_enabled:
            raise DevError(
                "TELEGRAM_DISABLED",
                "Telegram 通道默认关闭；须部署负责人启用后由房主安全配置",
                409,
            )

    def owner(self, raw, principal, model, *, enabled=True):
        args = validate(model, raw)
        principal, room = self.c.scope(principal, args)
        self.c.owner(principal)
        args["conversation_id"] = self.c.conversations.resolve(principal, room, args)[
            "id"
        ]
        if enabled:
            self.require_enabled()
            self.c.live_room(room)
        return args, principal, room

    def account(self, value, principal):
        row = self.store.one(
            "SELECT * FROM telegram_accounts WHERE id=? AND space_id=? AND owner_user_id=?",
            (value, principal.space_id, principal.user_id),
        )
        if not row:
            raise DevError(
                "TELEGRAM_ACCOUNT_NOT_FOUND", "当前房主没有此 Telegram 账号", 404
            )
        return row

    def binding(self, value, principal, room):
        row = self.store.one(
            "SELECT * FROM telegram_bindings WHERE id=? AND room_id=? AND space_id=? AND owner_user_id=?",
            (value, room["id"], principal.space_id, principal.user_id),
        )
        if not row:
            raise DevError(
                "TELEGRAM_BINDING_NOT_FOUND", "当前房间没有此 Telegram 绑定", 404
            )
        self.c.conversations.object(principal, row["conversation_id"])
        return row

    def valid(self, value, *, pairing=False):
        return self.c.channels.bindings.validate("telegram", value, pairing=pairing)

    def import_account(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.owner(raw, principal, models.AccountImport)
            token = args.pop("token").get_secret_value()
            TelegramAdapter(token)
            if token.split(":", 1)[0] != args["bot_id"]:
                raise DevError(
                    "TELEGRAM_BOT_MISMATCH", "令牌前缀与机器人编号不一致", 422
                )
            args["credential_digest"] = hashlib.sha256(token.encode()).hexdigest()

            def save():
                old = self.store.one(
                    "SELECT * FROM telegram_accounts WHERE bot_id=?", (args["bot_id"],)
                )
                if old and (
                    old["space_id"] != principal.space_id
                    or old["owner_user_id"] != principal.user_id
                ):
                    raise DevError(
                        "TELEGRAM_ACCOUNT_CONFLICT", "机器人属于另一个已认证身份", 409
                    )
                if (old["credential_version"] if old else 0) != args[
                    "expected_version"
                ]:
                    raise DevError("STALE_VERSION", "Telegram 账号版本已变化", 409)
                if (
                    not old
                    and self.store.one(
                        "SELECT COUNT(*) AS n FROM telegram_accounts WHERE owner_user_id=?",
                        (principal.user_id,),
                    )["n"]
                    >= 32
                ):
                    raise DevError(
                        "TELEGRAM_ACCOUNT_QUOTA", "机器人登记数量达到上限", 409
                    )
                aid, now = old["id"] if old else uuid.uuid4().hex, self.c.clock()
                generation = old["generation"] + 1 if old else 1
                self.store.execute(
                    """INSERT INTO telegram_accounts
                    (id,space_id,owner_user_id,bot_id,label,secret,state,credential_version,generation,created,updated)
                    VALUES (?,?,?,?,?,?,'active',?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    label=excluded.label,secret=excluded.secret,state='active',credential_version=excluded.credential_version,
                    generation=excluded.generation,poll_fence=poll_fence+1,poll_until=0,next_poll=0,reason_code='',
                    last_sync_at=NULL,updated=excluded.updated""",
                    (
                        aid,
                        principal.space_id,
                        principal.user_id,
                        args["bot_id"],
                        args["label"],
                        self.store.encrypt(token),
                        args["expected_version"] + 1,
                        generation,
                        now,
                        now,
                    ),
                )
                if old:
                    self.store.execute(
                        "UPDATE telegram_bindings SET state='revoked',version=version+1 WHERE account_id=?",
                        (aid,),
                    )
                self.c.audit(
                    room,
                    principal.actor,
                    "telegram.account_approved",
                    aid,
                    {"credential_version": args["expected_version"] + 1},
                )
                return {
                    "account_id": aid,
                    "credential_version": args["expected_version"] + 1,
                    "state": "active",
                    "verified_with_provider": False,
                }

            return self.c.mutation(principal, room, "telegram_account", args, save)

    def bind(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.owner(raw, principal, models.Bind)
            account = self.account(args["account_id"], principal)
            dot = self.c.dots.find(args["dot_id"])
            if (
                not dot
                or dot["room_id"] != room["id"]
                or dot["conversation_id"] != args["conversation_id"]
            ):
                raise DevError("TELEGRAM_DOT_SCOPE", "只能绑定当前话题的已有 dot", 403)
            slot = self.c.dot_chat.authorize(principal, room, dot)
            project = self.c.runtime.project(room["project_id"], principal)

            def save():
                if account["state"] != "active" or self.store.one(
                    "SELECT 1 FROM telegram_bindings WHERE account_id=? AND conversation_id=? AND state!='revoked'",
                    (account["id"], args["conversation_id"]),
                ):
                    raise DevError(
                        "TELEGRAM_BINDING_CONFLICT",
                        "此机器人已绑定当前话题；先明确撤销旧绑定",
                        409,
                    )
                bid, now, challenge = (
                    uuid.uuid4().hex,
                    self.c.clock(),
                    "CPT_" + secrets.token_urlsafe(24),
                )
                self.store.execute(
                    """INSERT INTO telegram_bindings
                    (id,account_id,space_id,owner_user_id,room_id,conversation_id,project_id,environment_id,dot_id,
                    owner_principal,dot_principal,project_snapshot,credential_version,generation,state,
                    pairing_hash,pairing_secret,pairing_until,mirror_enabled,disable_notification,expires_at,created,updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pairing',?,?,?,1,?,?,?,?)""",
                    (
                        bid,
                        account["id"],
                        principal.space_id,
                        principal.user_id,
                        room["id"],
                        args["conversation_id"],
                        room["project_id"],
                        room["environment_id"],
                        dot["id"],
                        principal_record(principal),
                        slot["principal"],
                        canonical(snapshot(project)),
                        account["credential_version"],
                        account["generation"],
                        digest(challenge),
                        self.store.encrypt(challenge),
                        now + 900,
                        int(args["disable_notification"]),
                        now + args["expires_seconds"],
                        now,
                        now,
                    ),
                )
                self.store.execute(
                    "UPDATE telegram_accounts SET poll_fence=poll_fence+1,poll_until=0,next_poll=0 WHERE id=?",
                    (account["id"],),
                )
                self.c.audit(
                    room,
                    principal.actor,
                    "telegram.binding_requested",
                    bid,
                    {"full_mirror": True, "automatic_execution": False},
                )
                return {
                    "binding_id": bid,
                    "version": 1,
                    "state": "pairing",
                    "pairing_command": "/start " + challenge,
                    "instructions": "向该机器人私聊发送配对命令，回面板核对实际 user_id 和 chat_id。一个私聊仅路由到一个协作室；其他协作室请使用另一个机器人。",
                }

            return self.c.mutation(principal, room, "telegram_bind", args, save)

    def control(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.owner(
                raw, principal, models.Control, enabled=False
            )
            row = self.binding(args["binding_id"], principal, room)
            if row["conversation_id"] != args["conversation_id"]:
                raise DevError("TELEGRAM_BINDING_SCOPE", "绑定不属于当前话题", 403)

            def save():
                row = self.binding(args["binding_id"], principal, room)
                if row["version"] != args["expected_version"]:
                    raise DevError("STALE_VERSION", "原绑定版本已变化", 409)
                user, chat = row["user_id"], row["chat_id"]
                if args["action"] == "activate":
                    self.valid(row["id"], pairing=True)
                    if (
                        row["state"] != "awaiting_owner"
                        or not args["confirm_same_owner"]
                        or not row["pending_user_id"]
                        or args["confirm_user_id"] != row["pending_user_id"]
                        or args["confirm_chat_id"] != row["pending_chat_id"]
                    ):
                        raise DevError(
                            "TELEGRAM_IDENTITY_UNCONFIRMED",
                            "须核对实际数字 user_id/chat_id 并确认是本人",
                            409,
                        )
                    user, chat, state = (
                        row["pending_user_id"],
                        row["pending_chat_id"],
                        "active",
                    )
                    if self.store.one(
                        "SELECT 1 FROM telegram_bindings WHERE account_id=? AND user_id=? AND chat_id=? AND state='active' AND id!=?",
                        (row["account_id"], user, chat, row["id"]),
                    ):
                        raise DevError(
                            "TELEGRAM_ROUTE_CONFLICT",
                            "此本人私聊已绑定另一个协作室；不能猜测当前房间",
                            409,
                        )
                elif args["action"] == "resume":
                    if row["state"] != "paused" or not user:
                        raise DevError(
                            "TELEGRAM_RESUME_DENIED", "只能恢复原已核对的暂停绑定", 409
                        )
                    if self.store.one(
                        "SELECT 1 FROM telegram_bindings WHERE account_id=? AND user_id=? AND chat_id=? AND state='active' AND id!=?",
                        (row["account_id"], user, chat, row["id"]),
                    ):
                        raise DevError(
                            "TELEGRAM_ROUTE_CONFLICT",
                            "原私聊已绑定另一个协作室，不能恢复混合路由",
                            409,
                        )
                    state = "active"
                elif args["action"] == "pause":
                    if row["state"] == "revoked":
                        raise DevError(
                            "TELEGRAM_REVOKED", "已撤销绑定不能暂停或恢复", 409
                        )
                    state = "paused"
                else:
                    state = "revoked"
                self.store.execute(
                    """UPDATE telegram_bindings SET state=?,user_id=?,chat_id=?,version=version+1,
                    pairing_hash='',pairing_secret='',updated=? WHERE id=?""",
                    (state, user, chat, self.c.clock(), row["id"]),
                )
                self.store.execute(
                    "UPDATE telegram_accounts SET poll_fence=poll_fence+1,poll_until=0,next_poll=0 WHERE id=?",
                    (row["account_id"],),
                )
                if state == "active":
                    self.valid(row["id"])
                self.c.channels.quarantine(
                    "telegram", row["id"], "binding_version_changed"
                )
                self.c.audit(
                    room,
                    principal.actor,
                    "telegram.binding_" + args["action"],
                    row["id"],
                    {"automatic_replay": False},
                )
                return {
                    "binding_id": row["id"],
                    "version": row["version"] + 1,
                    "state": state,
                    "historical_replay": False,
                }

            return self.c.mutation(principal, room, "telegram_control", args, save)

    def listing(self, raw, principal):
        args, principal, room = self.owner(raw, principal, models.Scope, enabled=False)
        accounts = self.store.all(
            """SELECT id,bot_id,label,state,credential_version,last_sync_at,reason_code
            FROM telegram_accounts WHERE space_id=? AND owner_user_id=? ORDER BY created DESC LIMIT 32""",
            (principal.space_id, principal.user_id),
        )
        bindings = self.store.all(
            """SELECT id,account_id,dot_id,version,generation,state,user_id,chat_id,
            pending_user_id,pending_chat_id,pairing_until,pairing_secret,disable_notification,expires_at,mirror_enabled
            FROM telegram_bindings WHERE room_id=? AND conversation_id=? ORDER BY created DESC LIMIT 64""",
            (room["id"], args["conversation_id"]),
        )
        for row in bindings:
            secret = row.pop("pairing_secret")
            row["pairing_command"] = (
                "/start " + self.store.decrypt(secret)
                if secret
                and row["state"] == "pairing"
                and row["pairing_until"] > self.c.clock()
                else ""
            )
        return {
            "enabled": self.c.config.telegram_enabled,
            "mode": "long_poll",
            "private_only": True,
            "accounts": accounts,
            "bindings": bindings,
            "deliveries": self.c.channels.listing(room["id"], args["conversation_id"]),
            "notification_note": "Telegram 无声发送仍可能显示通知；微信需在客户端设置免打扰。",
            **self.files.candidates(principal, room, args["conversation_id"]),
        }

    def claim_poll(self, aid):
        with self.store.transaction():
            self.require_enabled()
            row = self.store.one(
                "SELECT * FROM telegram_accounts WHERE id=? AND state='active'", (aid,)
            )
            now = self.c.clock()
            if not row or row["poll_until"] > now or row["next_poll"] > now:
                return None
            bindings = self.store.all(
                "SELECT id,version FROM telegram_bindings WHERE account_id=? AND state IN ('pairing','awaiting_owner','active')",
                (aid,),
            )
            valid = {}
            for b in bindings:
                try:
                    self.valid(b["id"], pairing=True)
                    valid[b["id"]] = b["version"]
                except DevError:
                    continue
            if not valid:
                self.store.execute(
                    "UPDATE telegram_accounts SET next_poll=?,reason_code='no_current_authorized_binding' WHERE id=?",
                    (now + 60, aid),
                )
                return None
            fence = row["poll_fence"] + 1
            self.store.execute(
                "UPDATE telegram_accounts SET poll_fence=?,poll_until=? WHERE id=?",
                (fence, now + 60, aid),
            )
            return {
                **row,
                "poll_fence": fence,
                "poll_until": now + 60,
                "bindings": valid,
            }

    def commit_updates(self, claim, updates):
        """Durable inbox + canonical messages + offset are one transaction."""
        with self.store.transaction():
            account = self.store.one(
                "SELECT * FROM telegram_accounts WHERE id=?", (claim["id"],)
            )
            if (
                not account
                or account["state"] != "active"
                or account["poll_fence"] != claim["poll_fence"]
                or account["poll_until"] <= self.c.clock()
                or account["credential_version"] != claim["credential_version"]
                or account["generation"] != claim["generation"]
            ):
                raise DevError(
                    "TELEGRAM_POLL_FENCE_LOST", "轮询原世代失效，不推进 offset", 409
                )
            for bid, version in claim["bindings"].items():
                binding, *_ = self.valid(bid, pairing=True)
                if binding["version"] != version:
                    raise DevError(
                        "TELEGRAM_BINDING_CHANGED", "轮询期间绑定已变化", 409
                    )
            if (
                not isinstance(updates, list)
                or len(updates) > 100
                or len(canonical(updates).encode()) > 2 * 1024 * 1024
            ):
                raise DevError("TELEGRAM_BATCH_LIMIT", "Telegram 收件批次超出预算", 413)
            ids, offset = [], account["offset"]
            for update in updates:
                if (
                    not isinstance(update, dict)
                    or type(update.get("update_id")) is not int
                    or update["update_id"] < 0
                ):
                    raise DevError(
                        "TELEGRAM_UPDATE_INVALID", "无效 update_id，不推进 offset", 422
                    )
                uid = update["update_id"]
                old = self.store.one(
                    "SELECT * FROM telegram_inbox WHERE account_id=? AND update_id=?",
                    (account["id"], uid),
                )
                if old:
                    if old["source_digest"] != digest(update):
                        raise DevError(
                            "TELEGRAM_UPDATE_CONFLICT", "重复 update_id 内容冲突", 409
                        )
                    if old["message_id"]:
                        ids.append(old["message_id"])
                    offset = max(offset, uid + 1)
                    continue
                if (
                    self.store.one(
                        "SELECT COUNT(*) AS n FROM telegram_inbox WHERE account_id=? AND created>?",
                        (account["id"], self.c.clock() - 86400),
                    )["n"]
                    >= 5000
                ):
                    raise DevError(
                        "TELEGRAM_INBOX_QUOTA",
                        "Telegram 持久收件预算达到上限，不推进 offset",
                        429,
                    )
                self.store.execute(
                    """INSERT INTO telegram_inbox
                    (account_id,update_id,credential_version,generation,source_digest,body,state,created)
                    VALUES (?,?,?,?,?,?,'saved',?)""",
                    (
                        account["id"],
                        uid,
                        account["credential_version"],
                        account["generation"],
                        digest(update),
                        self.store.encrypt(canonical(update)),
                        self.c.clock(),
                    ),
                )
                mid, reason = self.ingest(account, update, claim["bindings"])
                self.store.execute(
                    "UPDATE telegram_inbox SET state=?,message_id=?,reason_code=? WHERE account_id=? AND update_id=?",
                    (
                        "committed" if mid else "quarantined",
                        mid,
                        reason,
                        account["id"],
                        uid,
                    ),
                )
                if mid:
                    ids.append(mid)
                offset = max(offset, uid + 1)
            gap = bool(
                account["last_sync_at"]
                and self.c.clock() - account["last_sync_at"] >= 86400
            )
            self.store.execute(
                """UPDATE telegram_accounts SET offset=?,poll_until=0,next_poll=?,last_sync_at=?,
                reason_code=?,updated=? WHERE id=?""",
                (
                    offset,
                    self.c.clock() + 1,
                    self.c.clock(),
                    "retention_gap_possible" if gap else account["reason_code"],
                    self.c.clock(),
                    account["id"],
                ),
            )
            return {
                "committed": True,
                "message_ids": ids,
                "offset": offset,
                "retention_gap_possible": gap,
            }

    def ingest(self, account, update, eligible):
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return None, "unsupported_update"
        sender, chat = message.get("from", {}), message.get("chat", {})
        if (
            not isinstance(sender, dict)
            or not isinstance(chat, dict)
            or sender.get("is_bot") is not False
            or chat.get("type") != "private"
            or message.get("is_topic_message")
            or message.get("sender_chat")
        ):
            return None, "group_forum_or_bot_rejected"
        try:
            user_id, chat_id, external_id = (
                identifier(sender.get("id")),
                identifier(chat.get("id")),
                identifier(message.get("message_id")),
            )
        except DevError:
            return None, "invalid_identity"
        if user_id != chat_id or user_id == account["bot_id"]:
            return None, "private_identity_mismatch"
        value = message.get("text", message.get("caption", ""))
        if not isinstance(value, str) or len(value) > 24000:
            return None, "text_limit"
        if value.startswith("/start "):
            challenge = value[7:].strip()
            for bid in eligible:
                row = self.store.one(
                    "SELECT * FROM telegram_bindings WHERE id=?", (bid,)
                )
                if (
                    row["state"] == "pairing"
                    and row["pairing_until"] > self.c.clock()
                    and hmac.compare_digest(row["pairing_hash"], digest(challenge))
                ):
                    self.store.execute(
                        """UPDATE telegram_bindings SET state='awaiting_owner',pending_user_id=?,pending_chat_id=?,
                        pairing_hash='',pairing_secret='',version=version+1,updated=? WHERE id=?""",
                        (user_id, chat_id, self.c.clock(), bid),
                    )
                    return None, "pairing_waits_for_owner"
            return None, "pairing_unmatched"
        rows = self.store.all(
            "SELECT id FROM telegram_bindings WHERE account_id=? AND user_id=? AND chat_id=? AND state='active'",
            (account["id"], user_id, chat_id),
        )
        if len(rows) != 1 or rows[0]["id"] not in eligible:
            return None, "unbound_or_ambiguous_private_chat"
        binding, _, owner, room, dot = self.valid(rows[0]["id"])
        if self.store.one(
            "SELECT 1 FROM channel_message_map WHERE channel=? AND binding_id=? AND generation=? AND external_id=?",
            ("telegram", binding["id"], binding["generation"], "out:" + external_id),
        ):
            return None, "mirror_echo"
        old = self.store.one(
            "SELECT message_id FROM channel_message_map WHERE channel=? AND binding_id=? AND generation=? AND external_id=?",
            ("telegram", binding["id"], binding["generation"], "in:" + external_id),
        )
        if old and not update.get("edited_message"):
            return old["message_id"], "duplicate_external_message"
        reference = message.get("reply_to_message", {}).get("message_id")
        mapped = None
        if type(reference) is int:
            mapped = self.store.one(
                """SELECT message_id FROM channel_message_map WHERE channel='telegram'
                AND binding_id=? AND generation=? AND external_id IN (?,?)""",
                (
                    binding["id"],
                    binding["generation"],
                    "out:" + str(reference),
                    "in:" + str(reference),
                ),
            )
        previous = self.store.one(
            "SELECT * FROM collaboration_messages WHERE id=?",
            ((old or mapped or {}).get("message_id", ""),),
        )
        if previous and (
            previous["room_id"] != room["id"]
            or previous["conversation_id"] != binding["conversation_id"]
        ):
            raise DevError("TELEGRAM_REPLY_SCOPE", "原回复关联不在当前协作室", 403)
        if previous:
            self.c.chatroom.require_message_visible(owner, previous)
        if (
            self.store.one(
                "SELECT COUNT(*) AS n FROM collaboration_dot_messages WHERE dot_id=? AND state!='handled'",
                (dot["id"],),
            )["n"]
            >= 500
        ):
            raise DevError("TELEGRAM_DOT_QUOTA", "原 dot 收件积压达到上限", 429)
        mid, now = uuid.uuid4().hex, self.c.clock()
        files = attachment_manifest(message)
        body = {
            "body_text": redact(
                value or ("[附件消息]" if files else "[暂不支持的 Telegram 消息]")
            ),
            "mentions": [{"slot_id": dot["id"]}],
            "duplex_recipients": [dot["id"]],
            "provenance_project_ids": [room["project_id"]],
            "requires_owner_execution_request": True,
            "external_origin": {
                "schema_version": 1,
                "channel": "telegram",
                "binding_id": binding["id"],
                "account_id": account["id"],
                "generation": binding["generation"],
                "user_id": user_id,
                "peer_id": chat_id,
                "external_message_id": external_id,
                "execution_authority": False,
                "peer_pairing_verified": True,
                "supersedes_message_id": old["message_id"] if old else "",
            },
        }
        root = previous["thread_root_id"] if previous else mid
        self.store.execute(
            """INSERT INTO collaboration_messages
            (id,room_id,conversation_id,thread_id,thread_root_id,reply_to_id,author,origin,source_id,kind,body,state,created)
            VALUES (?,?,?,?,?,?,?,'external_channel',?,'text',?,'saved',?)""",
            (
                mid,
                room["id"],
                binding["conversation_id"],
                root,
                root,
                previous["id"] if previous else "",
                "telegram:" + account["id"] + ":" + digest(user_id)[:20],
                str(update["update_id"]),
                canonical(body),
                now,
            ),
        )
        self.store.execute(
            """INSERT INTO collaboration_dot_messages(dot_id,message_id,message_version,message_digest,created)
            VALUES (?,?,1,?,?)""",
            (dot["id"], mid, digest(body["body_text"]), now),
        )
        self.store.execute(
            """INSERT OR REPLACE INTO channel_message_map VALUES ('telegram',?,?,?,?)""",
            (binding["id"], binding["generation"], "in:" + external_id, mid),
        )
        stored = self.c.object("collaboration_messages", room, mid)
        body["notifications"] = self.c.chatroom.notify(
            room, stored, owner, body["mentions"]
        )
        self.store.execute(
            "UPDATE collaboration_messages SET body=? WHERE id=?",
            (canonical(body), mid),
        )
        for ordinal, item in enumerate(files):
            self.c.channels.media.add_telegram(binding, mid, ordinal, item)
        self.c.channels.queue(self.c.object("collaboration_messages", room, mid))
        return mid, "message_committed"

    def poll_failed(self, claim, error):
        with self.store.transaction():
            row = self.store.one(
                "SELECT * FROM telegram_accounts WHERE id=?", (claim["id"],)
            )
            if not row or row["poll_fence"] != claim["poll_fence"]:
                return
            code = error.code if isinstance(error, DevError) else "TELEGRAM_POLL_FAILED"
            delay = (
                max(5, error.details.get("retry_after_seconds", 5))
                if isinstance(error, DevError)
                else 5
            )
            state = (
                "attention"
                if isinstance(error, DevError) and error.status in {401, 403, 409, 422}
                else row["state"]
            )
            self.store.execute(
                "UPDATE telegram_accounts SET state=?,reason_code=?,next_poll=?,poll_until=0 WHERE id=?",
                (state, code, self.c.clock() + delay, row["id"]),
            )

    async def poll_once(self, aid):
        claim = await self.store.run(self.claim_poll, aid)
        if not claim:
            return
        try:
            client = self.client_factory(self.store.decrypt(claim["secret"]))
            # Read-only verification also prevents racing an existing webhook.
            await client.verify(claim["bot_id"])
            updates = await client.updates(claim["offset"])
            await self.store.run(self.commit_updates, claim, updates)
        except asyncio.CancelledError:
            await self.store.run(
                self.poll_failed,
                claim,
                DevError("TELEGRAM_POLL_CANCELLED", "轮询已停止", 503),
            )
            raise
        except Exception as exc:
            await self.store.run(self.poll_failed, claim, exc)

    async def tick(self):
        if not self.c.config.telegram_enabled:
            return
        rows = await self.store.run(
            lambda: self.store.all(
                """SELECT id FROM telegram_accounts
            WHERE state='active' AND next_poll<=? AND poll_until<=? ORDER BY next_poll,updated,id LIMIT 4""",
                (self.c.clock(), self.c.clock()),
            )
        )
        await asyncio.gather(*(self.poll_once(r["id"]) for r in rows))
