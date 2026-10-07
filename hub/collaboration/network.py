"""Pinned public callbacks and separately authorized registered probes.

Neither transport follows redirects, reads proxy settings, or persists bodies.
Every connection resolves and checks addresses again, then connects to an IP
while keeping the original Host and TLS server name.
"""
from __future__ import annotations
import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from shared.util import DevError


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes = b''


def callback_url(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or '%' in parsed.hostname
                or '\\' in value or not 1 <= (parsed.port or 443) <= 65535):
            raise ValueError()
        url = httpx.URL(value)
        if url.host != parsed.hostname or url.userinfo:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, httpx.InvalidURL):
        raise DevError('CALLBACK_URL_REJECTED', '回调必须是无凭据、无片段的有效 HTTPS 地址', 400) from None


async def resolve(host, port):
    loop = asyncio.get_running_loop()
    result = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({record[4][0] for record in result})


def public_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_reserved and not getattr(address, 'is_site_local', False)


async def callback_destination(url):
    parsed = callback_url(url)
    addresses = await resolve(parsed.hostname, parsed.port or 443)
    if not addresses or not all(public_address(address) for address in addresses):
        raise DevError('CALLBACK_ADDRESS_REJECTED', '回调解析到了非公网单播地址，连接已拒绝', 400)
    return httpx.URL(url).copy_with(host=addresses[0]), parsed.netloc, parsed.hostname


async def request(method, destination, headers=None, body=None, *, timeout=10, read_body=True):
    pinned_url, host_header, hostname = destination
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        outgoing = {'Host': host_header, 'Accept-Encoding': 'identity', **(headers or {})}
        async with client.stream(method, pinned_url, headers=outgoing, content=body,
                                 extensions={'sni_hostname': hostname}) as response:
            if 300 <= response.status_code < 400:
                raise DevError('HTTP_REDIRECT_REJECTED', '受限 HTTP 连接不跟随重定向', 400)
            chunks = bytearray()
            if read_body:
                async for chunk in response.aiter_raw():
                    chunks.extend(chunk)
                    if len(chunks) > 8192:
                        raise DevError('HTTP_RESPONSE_TOO_LARGE', '回调应答超过 8 KiB 上限', 413)
            return Reply(response.status_code, bytes(chunks))


async def webhook(url, body, headers):
    if not isinstance(body, bytes) or len(body) > 16384:
        raise DevError('EVENT_TOO_LARGE', '事件超过 16 KiB 上限', 413)
    async with asyncio.timeout(10):
        destination = await callback_destination(url)
        return await request('POST', destination, headers, body)


async def probe(config):
    # Private probe policy is separate from the public-only callback policy.
    from hub.gateway.network import pin
    async with asyncio.timeout(config['timeout_seconds']):
        destination = await pin(config['url'], config['private_networks'], allow_http=config['allow_http'])
        return await request('GET', destination, timeout=config['timeout_seconds'], read_body=False)
