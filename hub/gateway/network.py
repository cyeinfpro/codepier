"""Operator-approved immutable endpoints; connect to the validated IP, not a second DNS lookup."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx
from shared.util import DevError

PRIVATE = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', '::1/128', 'fc00::/7'))


def private_address(address):
    return any(address.version == network.version and address in network for network in PRIVATE)


def endpoint(value, networks, allow_http=False):
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
            raise ValueError()
        parts = urlsplit(value)
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username is not None or parts.password is not None or parts.fragment or parts.query or '%' in parts.hostname:
            raise ValueError()
        if parts.scheme == 'http' and not allow_http:
            raise ValueError()
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        if not 1 <= port <= 65535:
            raise ValueError()
        if len(networks) > 16:
            raise ValueError()
        approved = [ipaddress.ip_network(item, strict=True) for item in networks]
        # A private override cannot include metadata/link-local/multicast ranges
        # or use 0/0 as a blanket bypass. Public addresses need no override.
        if any(not any(n.version == p.version and n.subnet_of(p) for p in PRIVATE) for n in approved):
            raise ValueError()
        if parts.scheme == 'http' and not approved:
            raise ValueError()
        httpx.URL(value)
    except (ValueError, TypeError, httpx.InvalidURL) as exc:
        raise DevError('GATEWAY_ENDPOINT_INVALID', '需要无凭据/查询串的 HTTPS MCP 地址；HTTP 仅可显式批准指定内网范围') from exc
    return value


async def resolve(host, port):
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DevError('GATEWAY_DNS_FAILED', 'MCP 后端 DNS 解析失败', 502) from exc
    return sorted({row[4][0] for row in rows})


async def pin(value, networks, allow_http=False, resolver=resolve):
    endpoint(value, networks, allow_http)
    parts = urlsplit(value)
    addresses = await resolver(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80))
    if not addresses:
        raise DevError('GATEWAY_DNS_FAILED', 'MCP 后端没有可用地址', 502)
    approved = [ipaddress.ip_network(item) for item in networks]
    safe = []
    for text in addresses:
        try:
            address = ipaddress.ip_address(text)
        except ValueError as exc:
            raise DevError('GATEWAY_ADDRESS_DENIED', '后端解析到不允许的地址', 502) from exc
        if getattr(address, 'ipv4_mapped', None):
            address = address.ipv4_mapped
        local_ok = private_address(address) and any(address.version == n.version and address in n for n in approved)
        public_ok = address.is_global and not (address.is_multicast or address.is_reserved or getattr(address, 'is_site_local', False))
        if not (local_ok or public_ok) or (parts.scheme == 'http' and not local_ok):
            raise DevError('GATEWAY_ADDRESS_DENIED', '后端解析包含未批准的私网或保留地址；未连接', 502)
        safe.append(str(address))
    # HTTP Host and TLS SNI/certificate validation retain the configured host;
    # the TCP URL uses the exact validated address, closing DNS rebinding races.
    url = httpx.URL(value).copy_with(host=safe[0])
    return url, parts.netloc, parts.hostname
