"""Encrypted attachment payloads and bounded per-owner cache admission.

The plaintext marker is inside authenticated encryption. Legacy base64 text
remains readable, and key rotation operates on bytes rather than decoding UTF-8.
"""
import base64
import binascii
from cryptography.fernet import InvalidToken
import hashlib
from hub.artifacts import byte_range
from shared.util import DevError

MAX_CACHE = 256 * 1024 * 1024
PER_OWNER_CACHE = 64 * 1024 * 1024
UNATTACHED_TTL = 24 * 3600
MARKER = b"codepier-media-v1\0"


def encode(store, data):
    return store.cipher.encrypt(MARKER + data).decode('ascii')


def decode(store, value):
    try:
        data = store.cipher.decrypt(value.encode('ascii'))
        return data[len(MARKER):] if data.startswith(MARKER) else base64.b64decode(data, validate=True)
    except (InvalidToken, UnicodeError, binascii.Error) as exc:
        raise DevError('ATTACHMENT_CACHE_CORRUPT', '附件缓存完整性校验失败', 409) from exc


def decode_range(store, row, range_header=None):
    if row['state'] != 'ready' or not row['content']:
        raise DevError('ATTACHMENT_NOT_READY', '原附件尚不可用；此读取不会重跑生成任务', 409)
    data = decode(store, row['content'])
    if len(data) != row['size'] or hashlib.sha256(data).hexdigest() != row['sha256']:
        raise DevError('ATTACHMENT_CACHE_CORRUPT', '附件缓存完整性校验失败', 409)
    start, end, partial = byte_range(range_header, len(data))
    return data[start:end + 1], row, start, end, partial


def usage(store, *, space_id=None, owner_user_id=None, exclude_chunks=None):
    if (space_id is None) != (owner_user_id is None):
        raise ValueError('Cache ownership requires both space and user')
    scope = '' if space_id is None else ' AND space_id=? AND owner_user_id=?'
    args = () if space_id is None else (space_id, owner_user_id)
    ready = store.one("SELECT COALESCE(SUM(size),0) n FROM channel_attachments WHERE state='ready'" + scope, args)['n']
    query = 'SELECT COALESCE(SUM(c.bytes),0) n FROM channel_attachment_chunks c'
    if space_id is not None:
        query += ' JOIN channel_attachments a ON a.id=c.attachment_id'
    query += ' WHERE 1=1'
    if space_id is not None:
        query += ' AND a.space_id=? AND a.owner_user_id=?'
    if exclude_chunks is not None:
        query += ' AND c.attachment_id!=?'; args += (exclude_chunks,)
    return ready + store.one(query, args)['n']


def available(store, row, incoming, *, exclude_chunks=None):
    return (usage(store, space_id=row['space_id'], owner_user_id=row['owner_user_id'], exclude_chunks=exclude_chunks) + incoming <= PER_OWNER_CACHE
            and usage(store, exclude_chunks=exclude_chunks) + incoming <= MAX_CACHE)
