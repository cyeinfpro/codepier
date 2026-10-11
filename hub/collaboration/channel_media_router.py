"""Channel-neutral attachment access. Binary egress remains separately approved."""

import asyncio
import base64
import hashlib
import json
import uuid
from hub.artifacts import byte_range
from hub.collaboration.common import canonical
from hub.collaboration.telegram_protocol import DOWNLOAD_LIMIT, UPLOAD_LIMIT
from hub.principal import refresh_principal
from shared.util import DevError


class ChannelMediaRouter:
    def __init__(self, router):
        self.router, self.c, self.store = router, router.c, router.store
        self.legacy = self.c.weixin.media
        self.shares = self.legacy.shares
        self.attach_approved = self.legacy.attach_approved
        self.for_message = self.legacy.for_message
        self.for_messages = self.legacy.for_messages
        self.public = self.legacy.public

    def row(self, identifier, principal):
        found = self.store.one(
            "SELECT channel FROM channel_attachments WHERE id=?", (identifier,)
        )
        if not found or found["channel"] == "weixin":
            return self.legacy.row(identifier, principal)
        principal = refresh_principal(self.store, principal)
        row = self.store.one(
            "SELECT * FROM channel_attachments WHERE id=? AND space_id=? AND owner_user_id=?",
            (identifier, principal.space_id, principal.user_id),
        )
        if not row:
            raise DevError("ATTACHMENT_NOT_FOUND", "当前身份不能访问此附件", 404)
        self.c.runtime.project(row["project_id"], principal)
        self.c.conversations.object(principal, row["conversation_id"])
        room = self.store.one(
            "SELECT * FROM collaboration_rooms WHERE id=?", (row["room_id"],)
        )
        from hub.room_scope import require_room

        require_room(self.store, principal, room, row["conversation_id"])
        message = self.c.object("collaboration_messages", room, row["message_id"])
        self.c.chatroom.require_message_visible(principal, message)
        self.router.bindings.message(
            "telegram",
            row["binding_id"],
            message,
            generation=row["generation"],
            version=row["binding_version"],
        )
        if row["expires_at"] <= self.c.clock() or row["state"] == "expired":
            raise DevError("ATTACHMENT_EXPIRED", "附件已到保留期限", 410)
        return row

    def content(self, identifier, principal, range_header=None):
        found = self.store.one(
            "SELECT channel FROM channel_attachments WHERE id=?", (identifier,)
        )
        if not found or found["channel"] == "weixin":
            return self.legacy.content(identifier, principal, range_header)
        row = self.row(identifier, principal)
        if row["state"] != "ready" or not row["content"]:
            raise DevError("ATTACHMENT_NOT_READY", "附件尚未准备完成", 409)
        data = base64.b64decode(self.store.decrypt(row["content"]), validate=True)
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise DevError("ATTACHMENT_INTEGRITY", "附件完整性校验失败", 409)
        start, end, partial = byte_range(range_header, len(data))
        return data[start : end + 1], row, start, end, partial

    def add_telegram(self, binding, message_id, ordinal, item):
        aid, now = uuid.uuid4().hex, self.c.clock()
        oversize = item["size"] is not None and item["size"] > DOWNLOAD_LIMIT
        self.store.execute(
            """INSERT INTO channel_attachments
            (id,space_id,owner_user_id,room_id,conversation_id,project_id,message_id,ordinal,direction,
             binding_id,generation,peer_id,kind,name,mime,size,state,private_source,reason_code,expires_at,created,updated,channel)
            VALUES (?,?,?,?,?,?,?,?,'inbound',?,?,?,?,?,'application/octet-stream',?,?,?,?,?,?,?,'telegram')""",
            (
                aid,
                binding["space_id"],
                binding["owner_user_id"],
                binding["room_id"],
                binding["conversation_id"],
                binding["project_id"],
                message_id,
                ordinal,
                binding["id"],
                binding["generation"],
                binding["chat_id"],
                item["kind"],
                item["name"],
                item["size"],
                "unavailable" if oversize else "pending",
                self.store.encrypt(canonical({"file_id": item["file_id"]})),
                "telegram_download_limit" if oversize else "",
                min(binding["expires_at"], now + 7 * 86400),
                now,
                now,
            ),
        )
        self.store.execute(
            "UPDATE channel_attachments SET binding_version=? WHERE id=?",
            (binding["version"], aid),
        )
        return aid

    def cache_usage(self, exclude_chunks=None):
        # Shared with the legacy channel: every retained chunk consumes budget,
        # including paused/in-flight transfers. Call under the store transaction.
        ready = self.store.one(
            "SELECT COALESCE(SUM(size),0) AS n FROM channel_attachments WHERE state='ready'"
        )["n"]
        chunks = self.store.one(
            "SELECT COALESCE(SUM(bytes),0) AS n FROM channel_attachment_chunks"
            + (" WHERE attachment_id!=?" if exclude_chunks is not None else ""),
            (exclude_chunks,) if exclude_chunks is not None else (),
        )["n"]
        return ready + chunks

    def claim(self, aid):
        with self.store.transaction():
            row = self.store.one(
                "SELECT * FROM channel_attachments WHERE id=? AND channel='telegram'",
                (aid,),
            )
            if (
                not row
                or row["state"] != "pending"
                or row["next_attempt"] > self.c.clock()
            ):
                return None
            binding, account, owner, room, _ = self.router.bindings.validate(
                "telegram", row["binding_id"]
            )
            self.row(aid, owner)
            own_chunks = self.store.one(
                "SELECT COALESCE(SUM(bytes),0) AS n FROM channel_attachment_chunks WHERE attachment_id=?",
                (aid,),
            )["n"]
            remaining = max(
                0,
                (row["size"] if row["size"] is not None else DOWNLOAD_LIMIT)
                - own_chunks,
            )
            if self.cache_usage() + remaining > 256 * 1024 * 1024:
                self.store.execute(
                    "UPDATE channel_attachments SET next_attempt=?,reason_code='ATTACHMENT_CACHE_LIMIT',updated=? WHERE id=?",
                    (self.c.clock() + 300, self.c.clock(), aid),
                )
                return None
            fence = row["lease_fence"] + 1
            self.store.execute(
                "UPDATE channel_attachments SET state='fetching',lease_fence=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                (fence, self.c.clock() + 60, aid),
            )
            return {
                **row,
                "lease_fence": fence,
                "binding_version": binding["version"],
                "account": account,
                "owner": owner,
            }

    def commit(self, claim, data):
        with self.store.transaction():
            row = self.row(claim["id"], claim["owner"])
            binding, *_ = self.router.bindings.validate("telegram", row["binding_id"])
            if (
                row["state"] != "fetching"
                or row["lease_fence"] != claim["lease_fence"]
                or row["lease_until"] <= self.c.clock()
                or binding["version"] != claim["binding_version"]
            ):
                raise DevError("ATTACHMENT_FENCE_LOST", "原附件获取世代已变化", 409)
            if (
                len(data)
                > (UPLOAD_LIMIT if row["direction"] == "outbound" else DOWNLOAD_LIMIT)
                or row["size"] is not None
                and len(data) != row["size"]
            ):
                raise DevError("ATTACHMENT_SIZE_MISMATCH", "附件字节数不符合声明", 413)
            if (
                self.cache_usage(exclude_chunks=row["id"]) + len(data)
                > 256 * 1024 * 1024
            ):
                raise DevError(
                    "ATTACHMENT_CACHE_LIMIT", "附件缓存暂满；保留原分段等待空间", 429
                )
            if row["sha256"] and hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise DevError("ATTACHMENT_HASH_MISMATCH", "原产物内容摘要不匹配", 409)
            kind, mime = self.legacy.content_type(data)
            self.store.execute(
                """UPDATE channel_attachments SET state='ready',content=?,size=?,sha256=?,kind=?,mime=?,
                lease_until=0,reason_code='',updated=? WHERE id=?""",
                (
                    self.store.encrypt(base64.b64encode(data).decode()),
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    kind,
                    mime,
                    self.c.clock(),
                    row["id"],
                ),
            )

            self.store.execute(
                "DELETE FROM channel_attachment_chunks WHERE attachment_id=?",
                (row["id"],),
            )

    def current(self, claim):
        row = self.row(claim["id"], claim["owner"])
        if (
            row["state"] != "fetching"
            or row["lease_fence"] != claim["lease_fence"]
            or row["lease_until"] <= self.c.clock()
        ):
            raise DevError("ATTACHMENT_FENCE_LOST", "原附件获取世代失效", 409)
        return row

    def saved_chunks(self, claim):
        self.current(claim)
        values = []
        offset = 0
        for row in self.store.all(
            "SELECT * FROM channel_attachment_chunks WHERE attachment_id=? ORDER BY offset",
            (claim["id"],),
        ):
            data = base64.b64decode(self.store.decrypt(row["content"]), validate=True)
            if (
                row["offset"] != offset
                or len(data) != row["bytes"]
                or hashlib.sha256(data).hexdigest() != row["sha256"]
            ):
                raise DevError(
                    "ATTACHMENT_CACHE_CORRUPT", "缓存分段不连续或摘要错误", 409
                )
            values.append(data)
            offset += len(data)
        return values, offset

    def save_chunk(self, claim, offset, data):
        with self.store.transaction():
            row = self.current(claim)
            if not data or offset + len(data) > row["size"]:
                raise DevError("ATTACHMENT_CHUNK_INVALID", "产物分段超出原大小", 409)
            old = self.store.one(
                "SELECT sha256 FROM channel_attachment_chunks WHERE attachment_id=? AND offset=?",
                (row["id"], offset),
            )
            checksum = hashlib.sha256(data).hexdigest()
            if old and old["sha256"] != checksum:
                raise DevError("ATTACHMENT_CHUNK_CONFLICT", "同一产物位置字节改变", 409)
            if not old and self.cache_usage() + len(data) > 256 * 1024 * 1024:
                raise DevError(
                    "ATTACHMENT_CACHE_LIMIT", "附件缓存暂满；保留原分段等待空间", 429
                )
            self.store.execute(
                "INSERT OR IGNORE INTO channel_attachment_chunks VALUES (?,?,?,?,?)",
                (
                    row["id"],
                    offset,
                    len(data),
                    checksum,
                    self.store.encrypt(base64.b64encode(data).decode()),
                ),
            )
            self.store.execute(
                "UPDATE channel_attachments SET lease_until=? WHERE id=?",
                (self.c.clock() + 60, row["id"]),
            )

    def outbound_content(self, aid, binding_id):
        binding, _, owner, *_ = self.router.bindings.validate("telegram", binding_id)
        row = self.row(aid, owner)
        if (
            row["direction"] != "outbound"
            or row["binding_id"] != binding_id
            or row["peer_id"] != binding["chat_id"]
            or row["binding_version"] != binding["version"]
        ):
            raise DevError(
                "ATTACHMENT_EGRESS_NOT_APPROVED", "附件未批准发送到此原收件人", 403
            )
        artifact, _ = self.c.runtime.artifacts.row(row["artifact_id"], owner)
        if (
            artifact["sha256"] != row["sha256"]
            or artifact["bytes"] != row["size"]
            or artifact["expires"] <= self.c.clock()
        ):
            raise DevError(
                "ATTACHMENT_ARTIFACT_CHANGED", "已批准的原产物已改变或过期", 403
            )
        return self.content(aid, owner)[:2]

    async def materialize(self, aid):
        try:
            claim = await self.store.run(self.claim, aid)
        except DevError as exc:
            await self.store.run(
                lambda exc=exc: self.store.execute(
                    "UPDATE channel_attachments SET state='unavailable',reason_code=?,updated=? WHERE id=? AND channel='telegram' AND state='pending'",
                    (exc.code, self.c.clock(), aid),
                )
            )
            return
        if not claim:
            return
        try:
            if claim["direction"] == "inbound":
                client = self.c.telegram.client_factory(
                    self.store.decrypt(claim["account"]["secret"])
                )
                data = await client.download(
                    json.loads(self.store.decrypt(claim["private_source"]))["file_id"]
                )
            else:
                chunks, offset = await self.store.run(self.saved_chunks, claim)
                while offset < claim["size"]:

                    def inspect():
                        self.current(claim)
                        artifact, project = self.c.runtime.artifacts.row(
                            claim["artifact_id"], claim["owner"]
                        )
                        if (
                            artifact["sha256"] != claim["sha256"]
                            or artifact["bytes"] != claim["size"]
                        ):
                            raise DevError(
                                "ATTACHMENT_ARTIFACT_CHANGED",
                                "原产物在获取期间变化",
                                409,
                            )
                        return artifact, project

                    artifact, project = await self.store.run(inspect)
                    chunk = await self.c.runtime.artifacts.chunk(
                        artifact, project, offset
                    )
                    await self.store.run(self.save_chunk, claim, offset, chunk)
                    chunks.append(chunk)
                    offset += len(chunk)
                data = b"".join(chunks)
            await self.store.run(self.commit, claim, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:

            def fail(exc=exc):
                row = self.store.one(
                    "SELECT * FROM channel_attachments WHERE id=?", (aid,)
                )
                if row and row["lease_fence"] == claim["lease_fence"]:
                    quota = (
                        isinstance(exc, DevError)
                        and exc.code == "ATTACHMENT_CACHE_LIMIT"
                    )
                    terminal = not quota and (
                        row["attempts"] >= 5
                        or isinstance(exc, DevError)
                        and exc.status in {401, 403, 409, 410, 413, 422}
                    )
                    self.store.execute(
                        "UPDATE channel_attachments SET state=?,reason_code=?,next_attempt=?,lease_until=0 WHERE id=?",
                        (
                            "unavailable" if terminal else "pending",
                            exc.code
                            if isinstance(exc, DevError)
                            else "attachment_fetch_failed",
                            self.c.clock() + min(300, 5 * 2 ** min(row["attempts"], 6)),
                            aid,
                        ),
                    )

            await self.store.run(fail)

    async def tick(self):
        if not self.c.config.enabled:
            return
        await self.store.run(self.legacy.expire)

        def ready():
            self.store.execute(
                """UPDATE channel_attachments SET state='pending',lease_fence=lease_fence+1
                WHERE channel='telegram' AND state='fetching' AND lease_until<=?""",
                (self.c.clock(),),
            )
            return self.store.all(
                """SELECT id FROM channel_attachments WHERE channel='telegram' AND state='pending'
                AND next_attempt<=? ORDER BY next_attempt,updated,id LIMIT 4""",
                (self.c.clock(),),
            )

        rows = await self.store.run(ready)
        await asyncio.gather(*(self.materialize(row["id"]) for row in rows))
