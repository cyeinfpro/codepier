"""Command-only SSH over an already connected socket approved by readiness policy."""

from __future__ import annotations

import asyncio
import hmac
import importlib
import ipaddress
import math
import socket

from agent.ssh_terminal_transport import SSHTransportError, _ControlCapture, classify_error
from shared.ssh_host_pin import normalize_host_pin
from shared.ssh_terminal_config import TMUX_PROVISION_TIMEOUT_SECONDS, TerminalConfig


def _ip(value):
    # Kernel/SSH callbacks can spell IPv6 scope zones differently.
    return ipaddress.ip_address(value.split("%", 1)[0])


class _PinnedClient:
    def __init__(self, target, peer, pin, verify_host, password):
        self.target, self.peer, self.pin = target, peer, pin
        self.verify_host, self.load_password = verify_host, password
        self.candidate = None
        self.proved = False
        self.password_requested = False
        self.failure = None

    def validate_host_public_key(self, host, addr, port, key):
        try:
            presented = normalize_host_pin(key.get_fingerprint("sha256"))
            correct_endpoint = (
                host.casefold() == self.target.host.casefold()
                and port == self.target.port
                and _ip(addr) == self.peer
            )
        except (AttributeError, TypeError, ValueError):
            return False
        if not correct_endpoint or not hmac.compare_digest(presented, self.pin):
            self.failure = SSHTransportError("HOST_KEY_CHANGED", "目标 VPS 的 SSH 主机指纹不匹配")
            return False
        self.candidate = presented
        return True

    def prove_authenticated_peer(self):
        if self.candidate is None:
            raise SSHTransportError("HOST_KEY_UNKNOWN", "连接未完成指定主机密钥的核验")
        if not self.proved:
            self.verify_host(self.candidate)
            self.proved = True

    async def password_auth_requested(self):
        if self.password_requested:
            return None
        self.password_requested = True
        # AsyncSSH requests user authentication only after proving possession
        # of the exact pinned host key during key exchange.
        provider, self.load_password = self.load_password, None
        try:
            self.prove_authenticated_peer()
            value = await provider()
        except Exception as exc:
            self.failure = exc
            raise
        if not isinstance(value, str) or not value:
            self.failure = SSHTransportError("SSH_AUTH_FAILED", "SSH 密码不可用")
            raise self.failure
        return value


class ReadinessSSH:
    """One authenticated connection. No interactive shell, PTY, or forwarding API."""

    def __init__(self, connection, ssh, config):
        self._connection, self._ssh, self._config = connection, ssh, config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        connection, self._connection = self._connection, None
        if connection is None:
            return
        connection.close()
        try:
            await asyncio.wait_for(connection.wait_closed(), self._config.shutdown_timeout)
        except (TimeoutError, ConnectionError, OSError, asyncio.CancelledError) as exc:
            connection.abort()
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def run_control(self, command, *, wait_seconds=None, max_bytes=8192):
        """Capture bounded combined stdout/stderr from a fixed readiness command."""
        connection = self._connection
        if connection is None:
            raise SSHTransportError("SSH_CHANNEL_CLOSED", "SSH 连接已关闭")
        deadline = self._config.login_timeout if wait_seconds is None else wait_seconds
        if (
            not isinstance(command, str)
            or not command
            or len(command.encode("utf-8")) > 16_384
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 65_536
            or type(deadline) not in {int, float}
            or not math.isfinite(deadline)
            or not 0 < deadline <= TMUX_PROVISION_TIMEOUT_SECONDS
        ):
            raise ValueError("Unbounded readiness control request")
        result = asyncio.get_running_loop().create_future()
        result.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)

        class Capture(_ControlCapture, self._ssh.SSHClientSession):
            pass

        protocol = Capture(result, max_bytes)
        channel = None
        try:
            async with asyncio.timeout(deadline):
                channel, _ = await connection.create_session(
                    lambda: protocol,
                    command=command,
                    request_pty=False,
                    encoding=None,
                    window=min(max_bytes, self._config.ssh_window_bytes),
                    max_pktsize=min(max_bytes, self._config.frame_bytes),
                    x11_forwarding=False,
                )
                return await result
        except TimeoutError:
            raise SSHTransportError(
                "SSH_CONTROL_UNCERTAIN", "SSH 控制命令超时，不能确定远端结果"
            ) from None
        finally:
            channel = channel or protocol.channel
            if channel is not None:
                channel.close()
            if not result.done():
                result.cancel()


async def connect_readiness(target, sock, *, verify_host, password, configuration=None):
    """Adopt only the verified TCP socket; load a password at most once after KEX."""
    config = configuration or TerminalConfig.from_env()
    connection = client = None
    try:
        try:
            pin = normalize_host_pin(target.host_fingerprint)
        except ValueError:
            raise SSHTransportError("HOST_KEY_UNKNOWN", "缺少已独立核验的 SSH 主机指纹") from None
        peer, peer_port, *_ = sock.getpeername()
        if sock.family not in {socket.AF_INET, socket.AF_INET6} or peer_port != target.port:
            raise SSHTransportError("SSH_CONNECTION_FAILED", "目标 VPS 的已连接端口不匹配")
        try:
            ssh = importlib.import_module("asyncssh")
        except ImportError:
            raise SSHTransportError("SSH_DEPENDENCY_MISSING", "Hub 未安装 SSH 依赖") from None

        class Client(_PinnedClient, ssh.SSHClient):
            pass

        client = Client(target, _ip(peer), pin, verify_host, password)
        connection = await asyncio.wait_for(
            ssh.connect(
                host=target.host,
                port=target.port,
                username=target.username,
                sock=sock,
                config=None,
                known_hosts=b"",
                client_factory=lambda: client,
                # [] would load ~/.ssh default keys in AsyncSSH 2.24.1.
                client_keys=None,
                client_host_keys=None,
                client_host_keysign=False,
                agent_path=None,
                agent_identities=None,
                pkcs11_provider=None,
                password=None,
                preferred_auth=["password"],
                public_key_auth=False,
                host_based_auth=False,
                kbdint_auth=False,
                gss_auth=False,
                gss_kex=False,
                agent_forwarding=False,
                tunnel=None,
                proxy_command=None,
                canonicalize_hostname=False,
                server_host_key_algs="default",
                x509_trusted_certs=None,
                connect_timeout=config.connect_timeout,
                login_timeout=config.login_timeout,
            ),
            config.connect_timeout + config.login_timeout,
        )
        # Covers a successful SSH "none" user-auth without loading a password.
        client.prove_authenticated_peer()
        result = ReadinessSSH(connection, ssh, config)
        connection = None
        return result
    except asyncio.CancelledError:
        sock.close()
        raise
    except Exception as exc:
        sock.close()
        raise (getattr(client, "failure", None) or classify_error(exc)) from None
    finally:
        if client is not None:
            client.load_password = None
        if connection is not None:
            connection.abort()
