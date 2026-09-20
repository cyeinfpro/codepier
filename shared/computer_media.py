"""Bounded MCP image handling. Only explicit desktop results are promoted to images."""
from __future__ import annotations
import base64
import binascii
import copy
import hashlib
import json
import struct
import time
from shared.computer_contracts import COMPUTER_TOOLS
from shared.util import DevError, valid_json_value

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_CONTENT_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 100000
MEDIA_TTL_SECONDS = 900


def dimensions(raw: bytes, mime: str):
    if mime == 'image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n') and len(raw) >= 24:
        return struct.unpack('>II', raw[16:24])
    if mime == 'image/jpeg' and raw.startswith(b'\xff\xd8'):
        i = 2
        while i + 4 <= len(raw):
            if raw[i] != 255:
                break
            marker = raw[i + 1]
            if marker == 255:
                i += 1
                continue
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = int.from_bytes(raw[i+2:i+4], 'big')
            if length < 2 or i + 2 + length > len(raw):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 7:
                return (int.from_bytes(raw[i+7:i+9], 'big'), int.from_bytes(raw[i+5:i+7], 'big'))
            i += 2 + length
    raise DevError('COMPUTER_IMAGE_INVALID', '原生截图必须是有效 PNG 或 JPEG；未把未知数据作为图片返回')


def normalize_content(result: dict):
    if not isinstance(result, dict) or not valid_json_value(result):
        raise DevError('COMPUTER_PROTOCOL', '原生工具返回无效 JSON')
    source = result.get('content', [])
    if not isinstance(source, list) or len(source) > 32:
        raise DevError('COMPUTER_PROTOCOL', '原生结果内容数量无效')
    blocks, images, text, total, truncated = [], [], [], 0, False
    for block in source:
        if not isinstance(block, dict):
            raise DevError('COMPUTER_PROTOCOL', '原生内容块无效')
        if block.get('type') == 'image':
            mime, encoded = block.get('mimeType'), block.get('data')
            if mime not in {'image/png', 'image/jpeg'} or not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                raise DevError('COMPUTER_IMAGE_LIMIT', '原生截图类型或体积超出限制')
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise DevError('COMPUTER_IMAGE_INVALID', '原生截图 Base64 无效') from exc
            if len(raw) > MAX_IMAGE_BYTES or len(images) >= 4:
                raise DevError('COMPUTER_IMAGE_LIMIT', '截图超过单张 3 MiB 或每次 4 张限制')
            width, height = dimensions(raw, mime)
            if not 0 < width <= 32768 or not 0 < height <= 32768 or width * height > 64000000:
                raise DevError('COMPUTER_IMAGE_LIMIT', '截图像素数量异常')
            images.append({'mimeType': mime, 'width': width, 'height': height, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
            blocks.append({'type': 'image', 'mimeType': mime, 'data': encoded})
            total += len(encoded)
        elif block.get('type') == 'text' and isinstance(block.get('text'), str):
            remaining = max(0, MAX_TEXT_CHARS - sum(map(len, text)))
            value = block['text'][:remaining]
            truncated |= len(value) != len(block['text'])
            text.append(value)
            total += len(value.encode('utf-8'))
        else:
            # Resource links are never fetched: a provider cannot turn UI data into network/file access.
            raise DevError('COMPUTER_CONTENT_UNSUPPORTED', '原生工具返回未支持的内容块；没有自动读取资源链接')
        if total > MAX_CONTENT_BYTES:
            raise DevError('COMPUTER_IMAGE_LIMIT', '桌面返回内容超过 5 MiB')
    joined = '\n'.join(text)
    # Accessibility state detects structural/value changes without rejecting every blinking caret.
    fingerprint = hashlib.sha256(json.dumps({'text': joined, 'images': [(i['width'], i['height']) for i in images],
        'pixels': [] if joined.strip() else [i['sha256'] for i in images]}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'text': joined, 'images': images, '_computer_content': blocks,
            'content_truncated': truncated, 'native_is_error': bool(result.get('isError')),
            'computer_expires_at': time.time() + MEDIA_TTL_SECONDS, 'state_fingerprint': fingerprint}


def scrub_expired(value: dict, now: float | None = None):
    """Keep receipts, remove expired desktop text/pixels. Mutates a decoded result only."""
    now = time.time() if now is None else now
    data = value.get('data') if isinstance(value.get('data'), dict) else value
    expires = data.get('computer_expires_at')
    if isinstance(expires, (int, float)) and expires <= now:
        data.pop('_computer_content', None)
        data.pop('text', None)
        data.pop('action_result', None)
        data.pop('elements', None)
        data.pop('url', None)
        data.pop('title', None)
        data['media_expired'] = True
    return value


def mcp_result(name: str, value: dict):
    """Return metadata as JSON text and screenshots as native MCP image blocks, including polls."""
    public = copy.deepcopy(value)
    browser = name.startswith('browser_') or name in {'operations_get','operations_wait'} and public.get('tool','').startswith('browser_')
    if browser:
        nested=public.get('result')
        if name in {'operations_get','operations_wait'} and isinstance(nested,dict):scrub_expired(nested)
        else:scrub_expired(public)
    desktop = name in COMPUTER_TOOLS
    target = public
    if name in {'operations_get', 'operations_wait'} and public.get('tool') in COMPUTER_TOOLS:
        desktop = True
        nested = public.get('result')
        target = nested.get('data', {}) if isinstance(nested, dict) else {}
    patch = name == 'apply_patch' or name in {'operations_get', 'operations_wait'} and public.get('tool') == 'apply_patch'
    nested = public.get('result')
    patch_data = public if name == 'apply_patch' else nested.get('data', {}) if isinstance(nested, dict) else {}
    if not isinstance(patch_data, dict):
        patch_data = {}
    images = []
    if desktop and isinstance(target, dict):
        scrub_expired(target)
        supplied = target.pop('_computer_content', [])
        # Recheck stored/native bytes at the boundary; do not promote arbitrary file tool output.
        if supplied:
            checked = normalize_content({'content': supplied})
            images = checked['_computer_content']
        target.pop('state_fingerprint', None)
    error = desktop and bool(public.get('error')) and not public.get('pending')
    if desktop and public.get('state') in {'failed', 'cancelled', 'needs_review', 'interrupted'}:
        error = True
    if desktop and target.get('native_is_error'):
        error = True
    if patch and (patch_data.get('success') is False or public.get('state') in {'failed', 'needs_review', 'interrupted', 'cancelled'}):
        error = True
    return {'content': [{'type': 'text', 'text': json.dumps(public, ensure_ascii=False)}, *images],
            'structuredContent': public, 'isError': error}


def purge_database(db, table: str, now: float | None = None):
    """Incremental privacy cleanup; invoke inside the owner's DB lock/transaction."""
    if table not in {'calls', 'operations'}:
        raise ValueError('Unknown result table')
    now = time.time() if now is None else now
    # SQLite JSON functions are available in supported Python builds; avoid touching non-computer rows.
    rows = db.execute(f"SELECT id FROM {table} WHERE (tool LIKE 'computer_%' OR tool='browser_snapshot') AND result IS NOT NULL "
                      "AND json_extract(result,'$.data.computer_expires_at')<=? "
                      "AND COALESCE(json_extract(result,'$.data.media_expired'),0)=0 LIMIT 64", (now,)).fetchall()
    for row in rows:
        stored = db.execute(f'SELECT result FROM {table} WHERE id=?', (row['id'],)).fetchone()
        if not stored:
            continue
        result = scrub_expired(json.loads(stored['result']), now)
        db.execute(f'UPDATE {table} SET result=? WHERE id=?', (json.dumps(result, ensure_ascii=False), row['id']))
    return len(rows)
