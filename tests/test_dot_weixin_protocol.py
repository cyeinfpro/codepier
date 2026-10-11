"""Pinned wire compatibility, no real credentials or provider network requests."""
import asyncio
import base64
import json

import pytest

from hub.collaboration.weixin_protocol import (
    API_BASE, CDN_BASE, SOURCE_COMMIT, WeixinClient, WireReply, approved_url,
    uint64, strict_json, decode_key, encrypt_media, decrypt_media, safe_filename,
)
from shared.util import DevError


@pytest.mark.parametrize('value', [0, 9007199254740993, '18446744073709551615'])
def test_uint64_preserves_all_bits(value):
    assert uint64(value) == str(value)


@pytest.mark.parametrize('value', [True, 1.0, -1, '01', '1e3', '18446744073709551616', None])
def test_uint64_refuses_ambiguous_or_overflow_ids(value):
    with pytest.raises(DevError):
        uint64(value)


@pytest.mark.parametrize('body', [b'{"ret":0,"ret":1}', b'{"ret":NaN}', b'[]', b'not JSON'])
def test_untrustworthy_provider_json_is_not_success(body):
    with pytest.raises(DevError):
        strict_json(body)


@pytest.mark.parametrize('value', ['http://ilinkai.weixin.qq.com', 'https://ilinkai.weixin.qq.com.attacker.invalid',
    'https://user@ilinkai.weixin.qq.com', 'https://127.0.0.1', 'https://ilinkai.weixin.qq.com:444',
    'https://ilinkai.weixin.qq.com/#token', 'https://ilinkai.weixin.qq.com?token=secret'])
def test_provider_cannot_change_the_credential_destination(value):
    with pytest.raises(DevError):
        approved_url(value, bases=(API_BASE,), exact_base=True)


def test_aes_provider_vectors_roundtrip_raw_and_hex_keys():
    key = bytes(range(16))
    for size in [0, 1, 15, 16, 17, 32, 1024]:
        content = b'x' * size
        cipher = encrypt_media(content, key)
        assert len(cipher) == (size // 16 + 1) * 16
        assert decrypt_media(cipher, key) == content
    assert decode_key(base64.b64encode(key).decode()) == key
    assert decode_key(base64.b64encode(key.hex().encode()).decode()) == key
    with pytest.raises(DevError):
        decrypt_media(b'bad-ciphertext', key)
    assert safe_filename('../../folder\\secret.html') == 'secret.html'


def test_getupdates_uses_opaque_cursor_and_preserves_large_json_integer():
    calls = []
    async def transport(method, url, headers, body, **limits):
        calls.append((method, url, headers, json.loads(body)))
        return WireReply(200, b'{"ret":0,"msgs":[{"message_id":18446744073709551615}],"get_updates_buf":"opaque-new"}')
    client = WeixinClient(token='synthetic-provider-token', transport=transport)
    result = asyncio.run(client.updates('opaque-old'))
    assert uint64(result['msgs'][0]['message_id']) == '18446744073709551615'
    method, url, headers, body = calls[0]
    assert method == 'POST' and url == API_BASE + '/ilink/bot/getupdates'
    assert body['get_updates_buf'] == 'opaque-old' and 'sync_buf' not in body
    assert headers['AuthorizationType'] == 'ilink_bot_token'
    assert body['base_info']['channel_version'] == '2.4.10'
    assert SOURCE_COMMIT == 'c6643f6f0ac03574eb126f2d69569ea4267cc3c6'


def test_send_has_no_hidden_retries_or_delivery_claim():
    calls = []
    async def transport(method, url, headers, body, **limits):
        calls.append(json.loads(body))
        raise TimeoutError('lost response after remote may have committed')
    client = WeixinClient(token='synthetic-provider-token', transport=transport)
    with pytest.raises(TimeoutError):
        asyncio.run(client.send('peer', 'context', 'original-client-id', [{'type': 1, 'text_item': {'text': 'hello'}}]))
    assert len(calls) == 1
    assert calls[0]['msg']['client_id'] == 'original-client-id'
    assert calls[0]['msg']['context_token'] == 'context'


@pytest.mark.parametrize('response,code', [
    (WireReply(401, b'{}'), 'WEIXIN_AUTH_REJECTED'),
    (WireReply(429, b'{}', {'retry-after': '120'}), 'WEIXIN_RATE_LIMITED'),
    (WireReply(200, b'{"ret":-14}'), 'WEIXIN_SESSION_EXPIRED'),
    (WireReply(200, b'{"ret":-2}'), 'WEIXIN_SEND_UNCONFIRMED'),
    (WireReply(200, b'{"ret":0,"errcode":123}'), 'WEIXIN_SEND_UNCONFIRMED'),
    (WireReply(200, b'{"ret":0,"errcode":"0"}'), 'WEIXIN_SEND_UNCONFIRMED'),
    (WireReply(200, b'{"ret":0,"errcode":false}'), 'WEIXIN_SEND_UNCONFIRMED'),
    (WireReply(200, b'{}'), 'WEIXIN_SEND_UNCONFIRMED'),
])
def test_wire_errors_preserve_uncertainty(response, code):
    async def transport(*args, **kwargs):
        return response
    with pytest.raises(DevError) as caught:
        asyncio.run(WeixinClient(token='fixture', transport=transport).send('peer', 'context', 'client', [{'type': 1}]))
    assert caught.value.code == code
    if code == 'WEIXIN_RATE_LIMITED':
        assert caught.value.details['retry_after_seconds'] == 120


def test_cdn_upload_has_different_media_type_map_and_requires_receipt_header():
    calls = []
    async def transport(method, url, headers, body, **limits):
        calls.append((url, headers, body))
        if len(calls) == 1:
            return WireReply(200, json.dumps({'upload_full_url': CDN_BASE + '/upload?fixture=1'}).encode())
        return WireReply(200, b'', {'x-encrypted-param': 'encrypted-result'})
    item = asyncio.run(WeixinClient(token='fixture', transport=transport).upload_media(
        'peer', b'file-content', 'file', '../report.txt', filekey='persisted-filekey', key=b'k' * 16))
    assert json.loads(calls[0][2])['media_type'] == 3
    assert item['type'] == 4 and item['file_item']['len'] == '12'
    assert item['file_item']['file_name'] == 'report.txt'
    assert calls[1][1] == {'Content-Type': 'application/octet-stream'}  # no API token to CDN
    assert item['file_item']['media']['encrypt_query_param'] == 'encrypted-result'
