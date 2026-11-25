"""Pinned public callbacks and separately authorized registered probes.

Neither transport follows redirects, reads proxy settings, or persists bodies.
Every connection resolves and checks addresses again, then connects to an IP
while keeping the original Host and TLS server name.
"""

from __future__ import annotations
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import socket
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlsplit

import httpx
from shared.net_address import is_public_unicast_ip, parse_retry_after
from shared.util import DevError


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes = b""
    retry_after_seconds: float | None = None


def retry_after_seconds(value, *, now=None):
    """Only the bounded Retry-After value crosses the transport boundary."""
    return parse_retry_after(value, max_seconds=1800, now=now)


def callback_url(value):
    try:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
        ):
            raise ValueError()
        parsed = urlsplit(value)
        port = 443 if parsed.port is None else parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "%" in parsed.hostname
            or "\\" in value
            or not 1 <= port <= 65535
        ):
            raise ValueError()
        url = httpx.URL(value)
        if url.host != parsed.hostname or url.userinfo:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, httpx.InvalidURL):
        raise DevError(
            "CALLBACK_URL_REJECTED", "回调必须是无凭据、无片段的有效 HTTPS 地址", 400
        ) from None


async def resolve(host, port):
    loop = asyncio.get_running_loop()
    result = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({record[4][0] for record in result})


def public_address(value):
    return is_public_unicast_ip(value)


async def callback_destination(url):
    parsed = callback_url(url)
    addresses = await resolve(parsed.hostname, 443 if parsed.port is None else parsed.port)
    if not addresses or not all(public_address(address) for address in addresses):
        raise DevError("CALLBACK_ADDRESS_REJECTED", "回调解析到了非公网单播地址，连接已拒绝", 400)
    return httpx.URL(url).copy_with(host=addresses[0]), parsed.netloc, parsed.hostname


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


def _client():
    return httpx.AsyncClient(
        cookies=CookieJar(policy=_NoCookies()),
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=15),
    )


class PinnedClients:
    """Own small pools isolated by checked destination, SNI and policy."""

    def __init__(self, capacity=12):
        self.capacity = capacity
        self.entries = OrderedDict()

    @asynccontextmanager
    async def borrow(self, destination, policy):
        url, host, sni = destination
        key = (policy, url.scheme, url.host, url.port, host, sni)
        entry = self.entries.get(key)
        evicted = None
        if entry is None and len(self.entries) >= self.capacity:
            oldest_idle = next((k for k, value in self.entries.items() if not value[1]), None)
            if oldest_idle is not None:
                evicted = self.entries.pop(oldest_idle)[0]
        if entry is None and len(self.entries) < self.capacity:
            entry = [_client(), 0]
            self.entries[key] = entry
        if entry is None:
            # Existing callers also impose their own dispatch concurrency cap.
            async with _client() as client:
                yield client
            return
        entry[1] += 1
        self.entries.move_to_end(key)
        try:
            if evicted is not None:
                await evicted.aclose()
            yield entry[0]
        finally:
            entry[1] -= 1

    async def close(self):
        entries, self.entries = self.entries, OrderedDict()
        await asyncio.gather(*(entry[0].aclose() for entry in entries.values()))


async def request(
    method,
    destination,
    headers=None,
    body=None,
    *,
    timeout=10,  # noqa: ASYNC109 - outer webhook/probe enforces total budget.
    read_body=True,
    pool=None,
    policy="callback",
):  # noqa: ASYNC109 - Caller budget is enforced by the owned transport/process lifecycle.
    pinned_url, host_header, hostname = destination
    context = pool.borrow(destination, policy) if pool is not None else _client()
    async with context as client:
        outgoing = httpx.Headers(headers or {})
        outgoing["Host"] = host_header
        outgoing["Accept-Encoding"] = "identity"
        async with client.stream(
            method,
            pinned_url,
            headers=outgoing,
            content=body,
            extensions={"sni_hostname": hostname},
            timeout=timeout,
        ) as response:
            if 300 <= response.status_code < 400:
                raise DevError("HTTP_REDIRECT_REJECTED", "受限 HTTP 连接不跟随重定向", 400)
            chunks = bytearray()
            if read_body:
                async for chunk in response.aiter_raw():
                    chunks.extend(chunk)
                    if len(chunks) > 8192:
                        raise DevError("HTTP_RESPONSE_TOO_LARGE", "回调应答超过 8 KiB 上限", 413)
            elif pool is not None:
                length = response.headers.get("Content-Length", "")
                if (
                    length.isascii()
                    and length.isdecimal()
                    and len(length) <= 4
                    and int(length) <= 8192
                ):
                    # A small known-size body can be discarded to reuse the
                    # socket. Unknown/slow bodies do not delay status delivery.
                    consumed = 0
                    try:
                        async with asyncio.timeout(0.2):
                            async for part in response.aiter_raw():
                                consumed += len(part)
                                if consumed > 8192:
                                    break
                    except (TimeoutError, httpx.HTTPError):
                        pass
            delay = (
                retry_after_seconds(response.headers.get("Retry-After"))
                if response.status_code in {429, 503}
                else None
            )
            return Reply(response.status_code, bytes(chunks), delay)


async def webhook(url, body, headers, *, pool=None):
    if not isinstance(body, bytes) or len(body) > 16384:
        raise DevError("EVENT_TOO_LARGE", "事件超过 16 KiB 上限", 413)
    async with asyncio.timeout(10):
        destination = await callback_destination(url)
        return await request("POST", destination, headers, body, pool=pool)


async def probe(config, *, pool=None):
    # Private probe policy is separate from the public-only callback policy.
    from hub.gateway.network import pin

    async with asyncio.timeout(config["timeout_seconds"]):
        destination = await pin(
            config["url"], config["private_networks"], allow_http=config["allow_http"]
        )
        policy = ("probe", tuple(sorted(config["private_networks"])), config["allow_http"])
        return await request(
            "GET",
            destination,
            timeout=config["timeout_seconds"],
            read_body=False,
            pool=pool,
            policy=policy,
        )
