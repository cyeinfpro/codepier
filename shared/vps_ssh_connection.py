"""Single shared AsyncSSH connection factory for all CodePier VPS operations.

Used by Agent exec, Web SSH terminal, probe, tmux management, and Hub-native
readiness. Every connection explicitly passes known_hosts=None and config=None
so historical host fingerprints and local OpenSSH known_hosts/config files are
never read or enforced, while preserving SSH transport encryption, key exchange,
handshake signature verification, integrity checks, and account authentication.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import time
from typing import Any, Awaitable, Callable

from shared.vps_transport import (
    VPS_HOST_IDENTITY_VERIFICATION,
    VPS_SSH_PROTOCOL,
    VPS_TRANSPORT_RUNTIME,
)

_PASSWORD_PROMPT_RE = re.compile(r"password\s*:\s*$", re.I)


class VPSSSHConnectionError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        failure_stage: str = "connection",
        retryable: bool = False,
    ):
        self.code = code
        self.message = message
        self.failure_stage = failure_stage
        self.retryable = retryable
        super().__init__(message)


def asyncssh_available() -> bool:
    try:
        importlib.import_module("asyncssh")
        return True
    except ImportError:
        return False


def classify_vps_ssh_error(exc: BaseException) -> VPSSSHConnectionError:
    if isinstance(exc, VPSSSHConnectionError):
        return exc
    kind = type(exc).__name__
    if kind in {"PermissionDenied", "PasswordChangeRequired"}:
        return VPSSSHConnectionError(
            "SSH_AUTH_FAILED",
            "SSH 登录失败，请核对保存的凭据",
            failure_stage="authentication",
            retryable=False,
        )
    if kind in {
        "KeyExchangeFailed",
        "ProtocolError",
        "DisconnectError",
        "HostKeyNotVerifiable",
        "MACValidationError",
        "CompressionError",
    }:
        return VPSSSHConnectionError(
            "SSH_PROTOCOL_NEGOTIATION_FAILED",
            "SSH 协议握手或算法协商失败，请检查目标 SSH 服务配置",
            failure_stage="connection",
            retryable=False,
        )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return VPSSSHConnectionError(
            "SSH_CONNECT_TIMEOUT",
            "SSH 连接或认证超时",
            failure_stage="connection",
            retryable=True,
        )
    if kind in {"ConnectionLost", "ConnectionResetError", "BrokenPipeError"}:
        return VPSSSHConnectionError(
            "SSH_CHANNEL_CLOSED",
            "SSH 连接已断开",
            failure_stage="connection",
            retryable=True,
        )
    return VPSSSHConnectionError(
        "SSH_CONNECTION_FAILED",
        "SSH 连接失败，请检查目标地址、端口和网络",
        failure_stage="connection",
        retryable=True,
    )


class SavedPasswordAuthClient:
    """AsyncSSH client mixin for saved-VPS password / keyboard-interactive auth.

    Never reads local private keys, SSH agent, or known_hosts. Drops the secret
    reference immediately once sent, authenticated, lost, or finalized.
    """

    def __init__(
        self,
        password: str | None,
        *,
        challenge: Callable[[str, str, Any], Awaitable[Any]] | None = None,
    ):
        self._secret = password
        self._challenge = challenge
        self.sent_password = False
        self.challenge_rounds = 0
        self.failure: VPSSSHConnectionError | None = None
        self.authentication_method = "unknown"
        self.authenticated = False

    def drop_secret_reference(self) -> None:
        self._secret = None

    async def password_auth_requested(self):
        if self.sent_password or self._secret is None:
            return None
        self.sent_password = True
        self.authentication_method = "password"
        secret, self._secret = self._secret, None
        return secret

    def kbdint_auth_requested(self):
        if not self.sent_password and self._secret is not None:
            return ""
        if self._challenge is not None:
            return ""
        return None

    async def kbdint_challenge_received(self, name, instructions, lang, prompts):
        self.challenge_rounds += 1
        if self.challenge_rounds > 4 or len(prompts) > 8:
            return None
        if not prompts:
            return []
        if (
            not self.sent_password
            and self._secret is not None
            and len(prompts) == 1
            and not prompts[0][1]
            and _PASSWORD_PROMPT_RE.search(prompts[0][0])
        ):
            self.sent_password = True
            self.authentication_method = "keyboard-interactive"
            secret, self._secret = self._secret, None
            return [secret]
        if self._challenge is None:
            self.failure = VPSSSHConnectionError(
                "SSH_AUTH_CHALLENGE_REQUIRED",
                "SSH 服务器要求额外认证信息",
                failure_stage="authentication",
                retryable=False,
            )
            return None
        self.authentication_method = "keyboard-interactive"
        return await self._challenge(name, instructions, prompts)

    def auth_completed(self):
        self.authenticated = True
        if not self.sent_password and self.authentication_method == "unknown":
            self.authentication_method = "none"
        self.drop_secret_reference()

    def connection_lost(self, exc):
        self.drop_secret_reference()


class VPSSSHConnection:
    """Wrapper around an authenticated AsyncSSH connection with bounded close."""

    def __init__(
        self,
        connection: Any,
        *,
        authentication_method: str = "unknown",
        ssh_module: Any = None,
    ):
        self.connection = connection
        self.authentication_method = authentication_method
        self.ssh_module = ssh_module
        self.transport_contract = VPS_SSH_PROTOCOL
        self.transport_runtime = VPS_TRANSPORT_RUNTIME
        self.host_identity_verification_applied = VPS_HOST_IDENTITY_VERIFICATION
        self._closed = False

    @property
    def closed(self) -> bool:
        if self._closed or self.connection is None:
            return True
        is_closed = getattr(self.connection, "is_closed", None)
        return bool(is_closed()) if callable(is_closed) else False

    async def close(self, *, timeout: float = 5.0) -> str | None:  # noqa: ASYNC109
        """Bounded, diagnostic-only close. Never raises cleanup exceptions."""
        if self._closed:
            return None
        self._closed = True
        conn = self.connection
        if conn is None:
            return None
        try:
            conn.close()
            wait_closed = getattr(conn, "wait_closed", None)
            if callable(wait_closed):
                await asyncio.wait_for(wait_closed(), timeout=max(0.1, float(timeout)))
            return None
        except BaseException as exc:
            try:
                abort = getattr(conn, "abort", None)
                if callable(abort):
                    abort()
            except BaseException:
                pass
            return f"{type(exc).__name__}: {exc}"


def remaining_budget(deadline: float | None, *, now: float | None = None) -> float | None:
    if deadline is None:
        return None
    current = time.monotonic() if now is None else now
    return deadline - current


async def connect_vps(
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    connect_timeout: float = 10.0,
    login_timeout: float = 15.0,
    deadline: float | None = None,
    keepalive_interval: float | None = 15.0,
    keepalive_count_max: int = 3,
    challenge: Callable[[str, str, Any], Awaitable[Any]] | None = None,
    ssh_module: Any = None,
) -> VPSSSHConnection:
    """Establish a vps_ssh_v2 AsyncSSH connection with explicit known_hosts=None.

    No **kwargs are accepted so callers cannot override host trust parameters.
    """
    if ssh_module is None:
        try:
            ssh_module = importlib.import_module("asyncssh")
        except ImportError as exc:
            raise VPSSSHConnectionError(
                "SSH_DEPENDENCY_MISSING",
                "当前执行端尚未安装 AsyncSSH 依赖，请更新组件",
                failure_stage="connection",
                retryable=False,
            ) from exc

    rem = remaining_budget(deadline)
    if rem is not None and rem <= 0:
        raise VPSSSHConnectionError(
            "SSH_CONNECT_TIMEOUT",
            "SSH 连接前剩余超时预算已耗尽",
            failure_stage="connection",
            retryable=False,
        )

    eff_connect = float(connect_timeout)
    eff_login = float(login_timeout)
    if eff_connect <= 0 or eff_login <= 0:
        raise VPSSSHConnectionError(
            "SSH_CONNECT_TIMEOUT",
            "SSH 连接或认证超时预算不足",
            failure_stage="connection",
            retryable=False,
        )
    if rem is not None:
        eff_connect = min(eff_connect, rem)
        eff_login = min(eff_login, rem)
        overall_timeout = rem
    else:
        overall_timeout = eff_connect + eff_login

    class _Client(SavedPasswordAuthClient, ssh_module.SSHClient):
        pass

    client = _Client(password, challenge=challenge)
    connection = None
    try:
        connection = await asyncio.wait_for(
            ssh_module.connect(
                host,
                port=int(port),
                username=username,
                client_factory=lambda: client,
                known_hosts=None,
                config=None,
                server_host_key_algs="default",
                client_keys=None,
                client_host_keys=None,
                client_host_keysign=False,
                agent_path=None,
                agent_identities=None,
                pkcs11_provider=None,
                password=None,
                preferred_auth=["password", "keyboard-interactive"],
                public_key_auth=False,
                host_based_auth=False,
                gss_auth=False,
                gss_kex=False,
                agent_forwarding=False,
                connect_timeout=eff_connect,
                login_timeout=eff_login,
                keepalive_interval=keepalive_interval,
                keepalive_count_max=keepalive_count_max,
            ),
            timeout=overall_timeout,
        )
        return VPSSSHConnection(
            connection,
            authentication_method=client.authentication_method,
            ssh_module=ssh_module,
        )
    except asyncio.CancelledError:
        if connection is not None:
            await VPSSSHConnection(connection, ssh_module=ssh_module).close(timeout=2.0)
        raise
    except Exception as exc:
        if connection is not None:
            await VPSSSHConnection(connection, ssh_module=ssh_module).close(timeout=2.0)
        raise (client.failure or classify_vps_ssh_error(exc)) from None
    finally:
        client.drop_secret_reference()
