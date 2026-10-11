"""Telegram Bot API adapter. Tokens never appear in public errors or records."""

import json
import re
import secrets
from hub.collaboration.weixin_protocol import public_transport, safe_filename
from shared.util import DevError

API_BASE = "https://api.telegram.org"
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
DOWNLOAD_LIMIT = 20_000_000
UPLOAD_LIMIT = 50_000_000


def identifier(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise DevError("TELEGRAM_ID_INVALID", "Telegram 私聊身份编号无效", 422)
    return str(value)


def split_text(value, limit=3800):
    """Plain text, bounded in UTF-16 units; never split a Unicode code point."""
    parts, current, units = [], [], 0
    for char in value:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            parts.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        parts.append("".join(current))
    return parts


class TelegramAdapter:
    def __init__(self, token, *, transport=public_transport):
        if not re.fullmatch(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,200}", token):
            raise DevError("TELEGRAM_TOKEN_INVALID", "Telegram 凭据格式无效", 422)
        self.token, self.transport = token, transport

    async def call(
        self, method, payload, *, body=None, content_type="application/json"
    ):
        if method not in {
            "getMe",
            "getWebhookInfo",
            "getUpdates",
            "sendMessage",
            "getFile",
            "sendDocument",
        }:
            raise DevError("TELEGRAM_METHOD_DENIED", "不支持此 Telegram 操作", 403)
        # Fixed official origin, never accept user supplied proxy/file/token URL.
        try:
            wire = await self.transport(
                "POST",
                API_BASE + "/bot" + self.token + "/" + method,
                {"Content-Type": content_type},
                body
                if body is not None
                else json.dumps(payload, ensure_ascii=False).encode(),
                timeout=40,
                maximum=2 * 1024 * 1024,
            )
        except Exception:
            raise DevError(
                "TELEGRAM_TRANSPORT_UNKNOWN",
                "Telegram 请求结果未知；外发不会自动重试",
                502,
            ) from None
        try:
            result = json.loads(wire.body)
        except (ValueError, TypeError):
            raise DevError(
                "TELEGRAM_RESPONSE_UNKNOWN", "Telegram 未返回可验证的结果", 502
            ) from None
        if not isinstance(result, dict):
            raise DevError("TELEGRAM_RESPONSE_UNKNOWN", "Telegram 返回结构无效", 502)
        if wire.status == 429 or result.get("error_code") == 429:
            params = result.get("parameters")
            retry = params.get("retry_after", 60) if isinstance(params, dict) else 60
            retry = retry if type(retry) is int and 0 < retry <= 86400 else 60
            raise DevError(
                "TELEGRAM_RATE_LIMIT",
                "Telegram 限流；将在指定时间后继续原分项",
                429,
                retry_after_seconds=retry,
            )
        if result.get("ok") is not True or not 200 <= wire.status < 300:
            status = result.get("error_code", wire.status)
            if status in (400, 401, 403, 409):
                raise DevError(
                    "TELEGRAM_PROVIDER_REJECTED",
                    "Telegram 明确拒绝此操作，请核对账号或原目标",
                    status,
                )
            raise DevError(
                "TELEGRAM_RESPONSE_UNKNOWN", "Telegram 结果不确定，不自动重发", 502
            )
        return result.get("result")

    async def verify(self, bot_id):
        me = await self.call("getMe", {})
        webhook = await self.call("getWebhookInfo", {})
        if (
            not isinstance(me, dict)
            or identifier(me.get("id")) != bot_id
            or me.get("is_bot") is not True
        ):
            raise DevError(
                "TELEGRAM_BOT_MISMATCH", "凭据对应的机器人与登记编号不一致", 409
            )
        if not isinstance(webhook, dict) or webhook.get("url"):
            raise DevError(
                "TELEGRAM_WEBHOOK_CONFLICT",
                "已有 webhook；本版仅长轮询，不自动删除或覆盖已有配置",
                409,
            )

    async def updates(self, offset):
        return await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 25,
                "limit": 100,
                "allowed_updates": ["message", "edited_message"],
            },
        )

    async def send(self, chat_id, value, *, silent=False, reply_to=""):
        payload = {
            "chat_id": int(chat_id),
            "text": value,
            "disable_notification": bool(silent),
            "link_preview_options": {"is_disabled": True},
            "protect_content": True,
        }
        if reply_to:
            payload["reply_parameters"] = {
                "message_id": int(reply_to),
                "allow_sending_without_reply": True,
            }
        result = await self.call("sendMessage", payload)
        if (
            not isinstance(result, dict)
            or identifier(result.get("chat", {}).get("id")) != chat_id
        ):
            raise DevError(
                "TELEGRAM_RECEIPT_UNKNOWN",
                "Telegram 返回目标不一致，不能证明原投递成功",
                502,
            )
        return identifier(result.get("message_id"))

    async def send_document(
        self, chat_id, content, filename, *, silent=False, reply_to=""
    ):
        if not isinstance(content, bytes) or len(content) > UPLOAD_LIMIT:
            raise DevError(
                "TELEGRAM_UPLOAD_LIMIT", "附件超过50MB上限，请在面板读取", 413
            )
        boundary = "CodePier" + secrets.token_hex(16)
        fields = {
            "chat_id": chat_id,
            "disable_notification": "true" if silent else "false",
        }
        if reply_to:
            fields["reply_parameters"] = json.dumps(
                {"message_id": int(reply_to), "allow_sending_without_reply": True}
            )
        parts = []
        for name, value in fields.items():
            parts.append(
                (
                    "--"
                    + boundary
                    + '\r\nContent-Disposition: form-data; name="'
                    + name
                    + '"\r\n\r\n'
                    + value
                    + "\r\n"
                ).encode()
            )
        name = (
            safe_filename(filename)
            .replace('"', "_")
            .replace("\r", "_")
            .replace("\n", "_")
        )
        parts.append(
            (
                "--"
                + boundary
                + '\r\nContent-Disposition: form-data; name="document"; filename="'
                + name
                + '"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + content
            + b"\r\n"
        )
        parts.append(("--" + boundary + "--\r\n").encode())
        result = await self.call(
            "sendDocument",
            {},
            body=b"".join(parts),
            content_type="multipart/form-data; boundary=" + boundary,
        )
        if (
            not isinstance(result, dict)
            or identifier(result.get("chat", {}).get("id")) != chat_id
        ):
            raise DevError(
                "TELEGRAM_RECEIPT_UNKNOWN", "Telegram附件回执目标无法确认", 502
            )
        return identifier(result.get("message_id"))

    async def download(self, file_id):
        result = await self.call("getFile", {"file_id": file_id})
        path = result.get("file_path", "") if isinstance(result, dict) else ""
        size = result.get("file_size", 0) if isinstance(result, dict) else 0
        if (
            not re.fullmatch(r"[A-Za-z0-9_/-]+\.[A-Za-z0-9]+", path)
            or ".." in path.split("/")
            or not path
            or type(size) is not int
            or size > DOWNLOAD_LIMIT
        ):
            raise DevError(
                "TELEGRAM_FILE_UNAVAILABLE", "附件不在可下载预算或安全路径内", 413
            )
        try:
            wire = await self.transport(
                "GET",
                API_BASE + "/file/bot" + self.token + "/" + path,
                {},
                b"",
                timeout=40,
                maximum=DOWNLOAD_LIMIT,
            )
        except Exception:
            raise DevError(
                "TELEGRAM_FILE_UNAVAILABLE", "Telegram 附件获取失败；原消息保留", 502
            ) from None
        if wire.status != 200:
            raise DevError("TELEGRAM_FILE_UNAVAILABLE", "Telegram 附件暂不可用", 502)
        return wire.body


def attachment_manifest(message):
    """Only bounded descriptions, no provider file path or bearer URL."""
    items = []
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        items.append(("image", photos[-1]))
    for key, kind in [
        ("document", "file"),
        ("video", "video"),
        ("audio", "audio"),
        ("voice", "audio"),
    ]:
        if isinstance(message.get(key), dict):
            items.append((kind, message[key]))
    result = []
    for kind, item in items[:4]:
        file_id = item.get("file_id")
        if not isinstance(file_id, str) or not 0 < len(file_id) <= 512:
            continue
        size = item.get("file_size")
        result.append(
            {
                "kind": kind,
                "name": safe_filename(item.get("file_name", kind + "-attachment")),
                "size": size if type(size) is int and size >= 0 else None,
                "file_id": file_id,
            }
        )
    return result
