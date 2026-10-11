"""Conservative Tencent iLink wire adapter, independently implemented.

Reference: Tencent/openclaw-weixin c6643f6f0ac03574eb126f2d69569ea4267cc3c6,
package 2.4.10, docs/protocol.md. Client source is not a complete server SLA:
no native groups, outbound receipt lookup, client_id idempotency, delivery/read
proof, context TTL or arbitrary redirect is assumed. No automatic send retry.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hub.collaboration.network import callback_destination
from shared.util import DevError, valid_json_value

SOURCE_COMMIT = 'c6643f6f0ac03574eb126f2d69569ea4267cc3c6'
SOURCE_VERSION = '2.4.10'
API_BASE = 'https://ilinkai.weixin.qq.com'
CDN_BASE = 'https://novac2c.cdn.weixin.qq.com/c2c'
MAX_JSON = 4 * 1024 * 1024
MAX_MEDIA = 32 * 1024 * 1024
ITEM_NAMES = {2: 'image', 3: 'voice', 4: 'file', 5: 'video'}
UPLOAD_TYPES = {'image': 1, 'video': 2, 'file': 3, 'voice': 4}
MESSAGE_TYPES = {'image': 2, 'voice': 3, 'file': 4, 'video': 5}


def uint64(value):
    if type(value) is int:
        value = str(value)
    if (not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,19}', value)
            or int(value) > 18446744073709551615):
        raise DevError('WEIXIN_ID_INVALID', '微信消息编号必须是无损 uint64 十进制字符串，不能使用浮点数', 422)
    return value


def text(value, maximum, *, empty=False):
    if not isinstance(value, str) or not valid_json_value(value) or (not empty and not value) or len(value.encode()) > maximum or '\x00' in value:
        raise DevError('WEIXIN_FIELD_INVALID', '微信字段类型或长度无效', 422)
    return value


def strict_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_JSON:
        raise DevError('WEIXIN_RESPONSE_LIMIT', '微信 JSON 应答超过接收上限', 413)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('non-finite JSON number')

    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(result, dict) or not valid_json_value(result):
            raise ValueError('object required')
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DevError('WEIXIN_RESPONSE_INVALID', '微信应答不是唯一字段、有限数值的 JSON 对象', 502) from exc


def approved_url(value, *, bases, exact_base=False):
    try:
        text(value, 8192)
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.port not in (None, 443) or not parsed.hostname
                or any(ord(c) < 33 or ord(c) > 126 for c in value) or '\\' in value
                or '%' in parsed.path or any(p in {'.', '..'} for p in parsed.path.split('/'))):
            raise ValueError()
        for base in bases:
            allowed = urlsplit(base.rstrip('/'))
            if parsed.netloc != allowed.netloc:
                continue
            prefix = allowed.path.rstrip('/')
            if exact_base:
                if parsed.path.rstrip('/') == prefix and not parsed.query:
                    return value.rstrip('/')
            elif parsed.path.startswith(prefix + '/'):
                return value
        raise ValueError()
    except (ValueError, TypeError):
        raise DevError('WEIXIN_DESTINATION_DENIED', '目标不在本机明确批准的微信 API/CDN 清单内，未发送凭据或跟随重定向', 403) from None


@dataclass(frozen=True)
class WireReply:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)


async def public_transport(method, url, headers, body, *, timeout=40, maximum=MAX_JSON):
    """Re-resolve public addresses, pin the IP, preserve TLS SNI, no proxy/redirect."""
    async with asyncio.timeout(timeout):
        pinned, host, hostname = await callback_destination(url)
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout) as client:
            async with client.stream(method, pinned, headers={'Host': host, 'Accept-Encoding': 'identity', **headers},
                                     content=body, extensions={'sni_hostname': hostname}) as response:
                if 300 <= response.status_code < 400:
                    raise DevError('WEIXIN_REDIRECT_DENIED', '微信传输重定向未获独立批准', 403)
                chunks = bytearray()
                async for part in response.aiter_raw():
                    chunks.extend(part)
                    if len(chunks) > maximum:
                        raise DevError('WEIXIN_RESPONSE_LIMIT', '微信应答超过此次传输预算', 413)
                return WireReply(response.status_code, bytes(chunks),
                                 {k.lower(): v for k, v in response.headers.items() if k.lower() in
                                  {'retry-after', 'x-encrypted-param', 'content-type', 'content-length'}})


class WeixinClient:
    def __init__(self, api_base=API_BASE, *, token='', allowed_api_bases=(API_BASE,), transport=public_transport):
        self.base = approved_url(api_base, bases=allowed_api_bases, exact_base=True)
        self.token = text(token, 16384, empty=True)
        if any(ord(c) < 33 or ord(c) > 126 for c in self.token):
            raise DevError('WEIXIN_CREDENTIAL_INVALID', '微信凭据包含不可用于认证头的字符', 422)
        self.transport = transport

    def headers(self, *, auth=True, qr_status=False):
        result = {'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': str((2 << 16) | (4 << 8) | 10)}
        if not qr_status:
            result.update({'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token',
                           'X-WECHAT-UIN': base64.b64encode(str(int.from_bytes(os.urandom(4))).encode()).decode()})
        if auth:
            if not self.token:
                raise DevError('WEIXIN_AUTH_REQUIRED', '微信机器人凭据尚未确认', 403)
            result['Authorization'] = 'Bearer ' + self.token
        return result

    async def call(self, method, path, payload=None, *, auth=True, qr_status=False, timeout=40):
        if not path.startswith('/ilink/bot/') or '..' in path or '#' in path:
            raise DevError('WEIXIN_ENDPOINT_DENIED', '不允许任意微信 API 路径', 403)
        if payload is not None and auth:
            payload = {**payload, 'base_info': {'channel_version': SOURCE_VERSION, 'bot_agent': 'CodePier/1'}}
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode() if payload is not None else None
        if body is not None and len(body) > MAX_JSON:
            raise DevError('WEIXIN_REQUEST_LIMIT', '微信请求超过传输预算', 413)
        reply = await self.transport(method, self.base + path, self.headers(auth=auth, qr_status=qr_status),
                                     body, timeout=timeout, maximum=MAX_JSON)
        if reply.status in (401, 403):
            raise DevError('WEIXIN_AUTH_REJECTED', '微信凭据被拒绝，通道需要重新确认', 403)
        if reply.status == 429:
            value = reply.headers.get('retry-after', '')
            retry = min(3600, max(1, int(value))) if re.fullmatch(r'[0-9]{1,6}', value) else 60
            raise DevError('WEIXIN_RATE_LIMITED', '微信接口限流；未确认的发送不会自动重发', 429, retry_after_seconds=retry)
        if not 200 <= reply.status < 300:
            raise DevError('WEIXIN_HTTP_ERROR', '微信接口返回非成功 HTTP 状态', 502, upstream_status=reply.status)
        result = strict_json(reply.body)
        if result.get('ret') == -14 or result.get('errcode') == -14:
            raise DevError('WEIXIN_SESSION_EXPIRED', '微信会话失效，已停止使用原凭据', 403)
        return result

    async def updates(self, cursor):
        result = await self.call('POST', '/ilink/bot/getupdates', {'get_updates_buf': text(cursor, 65536, empty=True)})
        if type(result.get('ret')) is not int or result['ret'] != 0 or type(result.get('errcode', 0)) is not int or result.get('errcode', 0) != 0:
            raise DevError('WEIXIN_UPDATES_REJECTED', '微信收件请求未成功；保留原游标', 502)
        messages = result.get('msgs', [])
        if not isinstance(messages, list) or len(messages) > 100:
            raise DevError('WEIXIN_BATCH_LIMIT', '微信收件批次超过接纳上限；原游标未前移', 413)
        if 'get_updates_buf' not in result:
            raise DevError('WEIXIN_CURSOR_MISSING', '微信应答缺少原协议游标；不使用废弃 sync_buf 代替', 502)
        text(result['get_updates_buf'], 65536, empty=True)
        return result

    async def send(self, peer, context, client_id, items):
        text(peer, 256); text(context, 65536); text(client_id, 128)
        if not isinstance(items, list) or not 1 <= len(items) <= 32:
            raise DevError('WEIXIN_ITEMS_INVALID', '微信发送内容必须具有有界、有序的分项', 422)
        result = await self.call('POST', '/ilink/bot/sendmessage', {'msg': {'from_user_id': '',
            'to_user_id': peer, 'client_id': client_id, 'message_type': 2, 'message_state': 2,
            'context_token': context, 'item_list': items}})
        if (type(result.get('ret')) is not int or result['ret'] != 0
                or type(result.get('errcode', 0)) is not int or result.get('errcode', 0) != 0):
            raise DevError('WEIXIN_SEND_UNCONFIRMED', '微信未返回明确成功；保留原投递号等待核验，不自动重发', 409)
        message_id = result.get('message_id')
        if message_id is not None:
            # Outbound provider receipts may be opaque strings; only incoming
            # uint64 message identities use the numeric de-duplication contract.
            message_id = text(message_id, 256) if isinstance(message_id, str) else uint64(message_id)
        return {'provider_state': 'accepted', 'provider_message_id': message_id,
                'delivered_verified': False, 'read_verified': False}

    async def qr_start(self):
        return await self.call('POST', '/ilink/bot/get_bot_qrcode?bot_type=3', {'local_token_list': []}, auth=False)

    async def qr_status(self, qrcode, verify_code=''):
        values = {'qrcode': text(qrcode, 8192)}
        if verify_code:
            values['verify_code'] = text(verify_code, 32)
        return await self.call('GET', '/ilink/bot/get_qrcode_status?' + urlencode(values), auth=False, qr_status=True)

    async def download_media(self, media):
        if not isinstance(media, dict) or type(media.get('encrypt_type', 1)) is not int or media.get('encrypt_type', 1) != 1:
            raise DevError('WEIXIN_ENCRYPTION_UNSUPPORTED', '附件加密类型未经支持；未请求外部文件', 422)
        url = media.get('full_url') or CDN_BASE + '/download?' + urlencode({'encrypted_query_param': text(media.get('encrypt_query_param'), 8192)})
        approved_url(url, bases=(CDN_BASE,))
        reply = await self.transport('GET', url, {}, None, timeout=60, maximum=MAX_MEDIA + 16)
        if reply.status != 200:
            raise DevError('WEIXIN_MEDIA_DOWNLOAD_FAILED', '微信附件下载失败，未重新消费原消息', 502)
        key = decode_key(media.get('aes_key', ''))
        return decrypt_media(reply.body, key)

    async def upload_media(self, peer, content, kind, filename, *, filekey, key):
        if kind == 'voice':
            raise DevError('WEIXIN_VOICE_SEND_UNVERIFIED', '当前没有获验证的语音发送协议，未上传附件或发送消息', 409)
        if kind not in UPLOAD_TYPES or not isinstance(content, bytes) or not 0 < len(content) <= MAX_MEDIA:
            raise DevError('WEIXIN_MEDIA_LIMIT', '附件类型或大小超出当前微信传输预算', 413)
        cipher = encrypt_media(content, key)
        args = {'filekey': text(filekey, 128), 'media_type': UPLOAD_TYPES[kind], 'to_user_id': text(peer, 256),
                'rawsize': len(content), 'rawfilemd5': hashlib.md5(content, usedforsecurity=False).hexdigest(),
                'filesize': len(cipher), 'no_need_thumb': True, 'aeskey': key.hex()}
        prepared = await self.call('POST', '/ilink/bot/getuploadurl', args)
        if prepared.get('ret', 0) != 0 or prepared.get('errcode', 0) != 0:
            raise DevError('WEIXIN_UPLOAD_REJECTED', '微信附件上传参数未获准', 409)
        url = prepared.get('upload_full_url') or CDN_BASE + '/upload?' + urlencode({
            'encrypted_query_param': text(prepared.get('upload_param'), 8192), 'filekey': filekey})
        approved_url(url, bases=(CDN_BASE,))
        reply = await self.transport('POST', url, {'Content-Type': 'application/octet-stream'}, cipher, timeout=60, maximum=8192)
        encrypted_param = reply.headers.get('x-encrypted-param')
        if reply.status != 200 or not isinstance(encrypted_param, str) or not encrypted_param:
            raise DevError('WEIXIN_UPLOAD_UNCONFIRMED', '微信 CDN 没有返回明确的加密参数；未发送附件消息', 409)
        media = {'encrypt_query_param': text(encrypted_param, 8192),
                 'aes_key': base64.b64encode(key.hex().encode()).decode(), 'encrypt_type': 1}
        item = {'media': media}
        if kind == 'image':
            item['mid_size'] = len(cipher)
        elif kind == 'video':
            item['video_size'] = len(cipher)
        elif kind == 'file':
            item.update(file_name=safe_filename(filename), len=str(len(content)))
        else:
            raise DevError('WEIXIN_VOICE_SEND_UNVERIFIED', '当前只接收语音；未验证主动发送语音的服务端要求', 409)
        return {'type': MESSAGE_TYPES[kind], kind + '_item': item}


def safe_filename(value):
    value = text(value or 'attachment', 1024).replace('\\', '/').rsplit('/', 1)[-1]
    value = ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127 and c not in ':<>"|?*')
    return value[:180].strip(' .') or 'attachment'


def decode_key(value):
    try:
        raw = base64.b64decode(text(value, 128), validate=True)
        if len(raw) == 32:
            raw = bytes.fromhex(raw.decode('ascii'))
        if len(raw) != 16:
            raise ValueError()
        return raw
    except (ValueError, UnicodeError) as exc:
        raise DevError('WEIXIN_MEDIA_KEY_INVALID', '微信附件密钥格式无效；没有回退为未验证明文', 422) from exc


def encrypt_media(content, key):
    if not isinstance(content, bytes) or len(content) > MAX_MEDIA or not isinstance(key, bytes) or len(key) != 16:
        raise DevError('WEIXIN_MEDIA_INVALID', '微信附件内容或密钥不符合固定协议', 422)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(content) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_media(content, key):
    if not isinstance(content, bytes) or not content or len(content) > MAX_MEDIA + 16 or len(content) % 16:
        raise DevError('WEIXIN_MEDIA_INVALID', '微信附件密文长度无效', 422)
    try:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(content) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        result = unpadder.update(padded) + unpadder.finalize()
        if len(result) > MAX_MEDIA:
            raise ValueError()
        return result
    except ValueError as exc:
        raise DevError('WEIXIN_MEDIA_INVALID', '微信附件解密或完整块校验失败', 422) from exc
