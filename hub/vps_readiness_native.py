"""Closed-by-default Hub-native worker callbacks for VPS readiness.

The queue supplies references only. The application must inject the current
Space/grant resolver, a deferred secret lookup and a pinned SSH connector.
Neither an Agent nor a second hostname lookup may substitute for that connector.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import hmac
import ipaddress
import re
import socket

from agent.ssh_terminal_tmux import probe_tmux
from agent.ssh_terminal_transport import SSHTransportError
from shared.ssh_host_pin import normalize_host_pin


_MAX_DNS_ANSWERS = 16
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)
_TRANSLATION_NETS = tuple(
    ipaddress.ip_network(value)
    for value in ("64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16")
)
# 168.63.129.16 is a platform virtual address (Azure WireServer); it looks
# globally routable to the address library. Do not allow it out of the Hub.
_PLATFORM_ADDRESSES = {ipaddress.ip_address("168.63.129.16")}
_AUTHORITY = (
    "space_id",
    "vps_id",
    "owner_user_id",
    "connection_revision",
    "grant_id",
    "authorized_user_id",
)


class ReadinessNativeError(Exception):
    """Only a fixed local result; do not carry connector or credential text."""

    def __init__(self, result, code=None):
        super().__init__(code or result)
        self.result = result
        self.code = code or result


def _ip(value):
    if not isinstance(value, str) or "%" in value:
        raise ValueError("expected a numeric IP without a zone")
    address = ipaddress.ip_address(value)
    return getattr(address, "ipv4_mapped", None) or address


def _host(value):
    try:
        return str(_ip(value))
    except ValueError:
        pass
    if not isinstance(value, str):
        raise ValueError("invalid host")
    name = value.rstrip(".").encode("idna").decode("ascii").lower()
    labels = name.split(".")
    if len(name) > 253 or len(labels) < 2 or not all(map(_LABEL.fullmatch, labels)):
        raise ValueError("invalid host")
    return name


def _port(value):
    return type(value) is int and 1 <= value <= 65535


@dataclass(frozen=True, repr=False)
class NativeSSHTarget:
    """Authorized inventory values only, never browser request host values."""

    job_id: str
    space_id: str
    vps_id: str
    owner_user_id: str
    connection_revision: int
    grant_id: str
    authorized_user_id: str
    host: str
    port: int
    username: str
    host_fingerprint: str


@dataclass(frozen=True)
class PrivateSSHEndpoint:
    """Narrow deployment exception, never a flag from a VPS entry or API."""

    space_id: str
    host: str
    port: int
    address: str

    def __post_init__(self):
        ip = _ip(self.address)
        if (
            not isinstance(self.space_id, str)
            or not self.space_id
            or not _port(self.port)
            or not any(ip in network for network in _PRIVATE_NETS)
        ):
            raise ValueError("private SSH exception must name one Space, host, port and private IP")
        object.__setattr__(self, "host", _host(self.host))
        object.__setattr__(self, "address", str(ip))


async def _resolve(host, port):
    # Use an absolute DNS name: don't append Hub-local search suffixes.
    try:
        address = _ip(host)
    except ValueError:
        pass
    else:
        return (str(address),)
    records = await asyncio.get_running_loop().getaddrinfo(
        host + ".", port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return tuple(record[4][0] for record in records)


async def _dial(address, port):
    ip = _ip(address)
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(sock, (str(ip), port))
        return sock
    except BaseException:
        sock.close()
        raise


class NativeVPSReadiness:
    """VPSReadiness callback implementation with an injected identity-verified connector.

    resolve_target(job, action) asynchronously re-resolves the exact current
    record and active grant, returning NativeSSHTarget or None. The trusted SSH
    connector receives only the vetted already-connected socket and saved
    original host identity. It must call verify_host with the cryptographically
    proven SHA256 fingerprint before awaiting password. It must never fall back
    to another socket, resolver, agent, known_hosts mode or authentication path.
    """

    def __init__(
        self,
        *,
        resolve_target,
        secret,
        connector=None,
        provisioner=None,
        resolver=None,
        dial=None,
        private_allowlist=(),
        connect_timeout=10,
        authentication_timeout=15,
        close_timeout=5,
    ):
        if not all(
            0 < timeout <= 120
            for timeout in (connect_timeout, authentication_timeout, close_timeout)
        ):
            raise ValueError("invalid native SSH timeout")
        self._resolve_target = resolve_target
        self._secret = secret
        self._connector = connector
        # The helper MUST await the final authorization callback just before
        # starting its remote package command. Never call a sync-only helper
        # with an async callback (it could run without enforcing the fence).
        self._provision = provisioner
        self._resolve = resolver or _resolve
        self._dial = dial or _dial
        rules = tuple(private_allowlist)
        if any(not isinstance(rule, PrivateSSHEndpoint) for rule in rules):
            raise ValueError("expected exact PrivateSSHEndpoint rules")
        self._private_allowlist = frozenset(rules)
        self._connect_timeout = connect_timeout
        self._authentication_timeout = authentication_timeout
        self._close_timeout = close_timeout

    async def _current(self, job, action, expected=None):
        target = await self._resolve_target(job, action)
        if (
            not isinstance(target, NativeSSHTarget)
            or not isinstance(target.job_id, str)
            or not target.job_id
            or target.job_id != getattr(job, "id", None)
            or any(getattr(target, field) != getattr(job, field, None) for field in _AUTHORITY)
            or not all(
                isinstance(getattr(target, field), str) and getattr(target, field)
                for field in _AUTHORITY
                if field != "connection_revision"
            )
            or type(target.connection_revision) is not int
            or target.connection_revision < 1
            or (expected is not None and target != expected)
        ):
            raise ReadinessNativeError("denied")
        try:
            if (
                not _port(target.port)
                or _host(target.host) != target.host
                or not isinstance(target.username, str)
                or not 0 < len(target.username) <= 128
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in target.username)
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ReadinessNativeError("denied", "HUB_SSH_TARGET_INVALID") from None
        try:
            if normalize_host_pin(target.host_fingerprint) != target.host_fingerprint:
                raise ValueError
        except ValueError:
            raise ReadinessNativeError("denied", "HUB_SSH_TRUST_MIGRATION_REQUIRED") from None
        return target

    async def require_target(self, job, action):
        """A caller may surface the fixed trust/transport status without probing."""
        if action not in {"inspect", "install"}:
            raise ReadinessNativeError("denied")
        return await self._current(job, action)

    async def authorize(self, job, action):
        try:
            await self.require_target(job, action)
        except Exception:
            return False
        return True

    def _allowed(self, target, ip):
        if ip in _PLATFORM_ADDRESSES or any(ip in network for network in _TRANSLATION_NETS):
            return False
        if any(ip in network for network in _PRIVATE_NETS):
            return (
                PrivateSSHEndpoint(target.space_id, target.host, target.port, str(ip))
                in self._private_allowlist
            )
        return bool(
            ip.is_global
            and not (
                ip.is_multicast
                or ip.is_unspecified
                or ip.is_reserved
                or ip.is_link_local
                or ip.is_loopback
            )
        )

    async def _approved_addresses(self, target):
        answers = await self._resolve(target.host, target.port)
        if not isinstance(answers, (tuple, list)) or not answers or len(answers) > _MAX_DNS_ANSWERS:
            raise ReadinessNativeError("offline" if not answers else "denied")
        try:
            ips = tuple(dict.fromkeys(_ip(value) for value in answers))
        except ValueError:
            raise ReadinessNativeError("denied") from None
        # Reject the entire answer set on a disallowed result. No filtering or
        # choosing a convenient DNS answer that could hide a rebinding.
        if any(not self._allowed(target, ip) for ip in ips):
            raise ReadinessNativeError("denied")
        return ips

    async def _open_socket(self, job, action, target):
        async with asyncio.timeout(self._connect_timeout):
            ips = await self._approved_addresses(target)
            for ip in ips:
                await self._current(job, action, target)
                try:
                    sock = await self._dial(str(ip), target.port)
                except OSError:
                    continue
                try:
                    peer = sock.getpeername()
                    if _ip(peer[0]) != ip or peer[1] != target.port:
                        raise ReadinessNativeError("denied")
                    return sock
                except BaseException:
                    sock.close()
                    raise
            raise ReadinessNativeError("offline")

    @asynccontextmanager
    async def _connection(self, job, action):
        target = await self._current(job, action)
        if self._connector is None:
            raise ReadinessNativeError("unsupported")
        sock = transport = None
        trusted = secret_released = False

        def verify_host(fingerprint):
            nonlocal trusted
            try:
                candidate = normalize_host_pin(fingerprint)
            except ValueError:
                trusted = False
                raise ReadinessNativeError("denied") from None
            trusted = hmac.compare_digest(candidate, target.host_fingerprint)
            if not trusted:
                raise ReadinessNativeError("denied")

        async def password():
            nonlocal secret_released
            if not trusted or secret_released:
                raise ReadinessNativeError("denied")
            # A changed/revoked record is rejected even after key exchange.
            await self._current(job, action, target)
            secret_released = True
            value = await self._secret(target)
            await self._current(job, action, target)
            if not trusted or not isinstance(value, str) or not 0 < len(value) <= 4096:
                raise ReadinessNativeError("denied")
            return value

        try:
            sock = await self._open_socket(job, action, target)
            await self._current(job, action, target)
            transport = await asyncio.wait_for(
                self._connector(target, sock, verify_host=verify_host, password=password),
                self._authentication_timeout,
            )
            if not trusted:
                raise ReadinessNativeError("denied")
            await self._current(job, action, target)
            yield target, transport
        finally:
            await self._close_connection(sock, transport)

    async def _close_connection(self, sock, transport):
        try:
            if transport is not None:
                with suppress(Exception):
                    await asyncio.wait_for(transport.close(), self._close_timeout)
        finally:
            if sock is not None:
                sock.close()

    @staticmethod
    def _transport_status(error):
        code = getattr(error, "code", "")
        terminal_failures = {
            "SSH_AUTH_FAILED": "ssh_auth_failed",
            "HOST_KEY_UNKNOWN": "host_key_unknown",
            "HOST_KEY_CHANGED": "host_key_changed",
            "HOST_KEY_REVOKED": "host_key_revoked",
            "HOST_TRUST_CONFIGURATION": "host_key_unknown",
            "AUTH_REVOKED": "authorization_changed",
        }
        if code in terminal_failures:
            return terminal_failures[code]
        if code == "TMUX_INSTALL_PERMISSION":
            return "denied"
        if code in {"SSH_DEPENDENCY_MISSING", "TMUX_INSTALL_UNAVAILABLE"}:
            return "unsupported"
        if code in {"SSH_CONNECT_TIMEOUT", "SSH_CHANNEL_CLOSED", "SSH_CONNECTION_FAILED"}:
            return "offline"
        if code == "TMUX_INSTALL_UNCERTAIN":
            return "uncertain"
        return "failed"

    async def inspect(self, job):
        try:
            async with self._connection(job, "inspect") as (_, transport):
                result = await probe_tmux(transport)
                if result.get("available") is True:
                    return "ready"
                if result.get("reason") == "TMUX_NOT_INSTALLED":
                    return "missing"
                return "unsupported"
        except ReadinessNativeError as error:
            return error.result
        except (TimeoutError, OSError):
            return "offline"
        except SSHTransportError as error:
            result = self._transport_status(error)
            return "failed" if result == "uncertain" else result
        except Exception:
            return "failed"

    async def provision(self, job):
        try:
            if self._provision is None:
                await self.require_target(job, "install")
                return "unsupported"
            async with self._connection(job, "install") as (target, transport):

                async def authorize_install():
                    await self._current(job, "install", target)
                    return True

                result = await self._provision(
                    transport,
                    (target.host, target.port, target.username),
                    authorize=authorize_install,
                )
                return "ok" if result.get("available") is True else "unsupported"
        except ReadinessNativeError as error:
            return "failed" if error.result == "offline" else error.result
        except (TimeoutError, OSError):
            return "failed"
        except SSHTransportError as error:
            # Built-in installer exit 77/69 proves the package manager was
            # never invoked; generic failures may have partially changed it.
            if error.code == "TMUX_INSTALL_PERMISSION":
                return "install_not_started_denied"
            if error.code == "TMUX_INSTALL_UNAVAILABLE":
                return "install_not_started_unsupported"
            result = self._transport_status(error)
            return "failed" if result == "offline" else result
        except Exception:
            return "failed"
