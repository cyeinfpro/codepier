"""AsyncSSH byte-channel adapter. No local shell or private key discovery."""

from __future__ import annotations

import asyncio
from collections import deque
import importlib
import re

from .ssh_terminal_trust import HostKeyPolicy, HostTrustError
from shared.vps_ssh_connection import VPSSSHConnectionError, connect_vps
from shared.vps_transport import VPS_HOST_IDENTITY_VERIFICATION, VPS_SSH_PROTOCOL


class SSHTransportError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def available() -> bool:
    try:
        importlib.import_module("asyncssh")
        return True
    except ImportError:
        return False


def _classify_host_key_error(exc):
    # AsyncSSH 2.24.1 checks its revoked keys before calling our validator.
    # This fixed library reason only affects the UI classification, never trust.
    if str(exc).startswith("Host key is revoked for host "):
        return SSHTransportError("HOST_KEY_REVOKED", "主机密钥已撤销")
    return SSHTransportError("HOST_KEY_UNKNOWN", "无法核验 SSH 主机密钥或协商算法")


def classify_error(exc, trust=None):
    if isinstance(exc, SSHTransportError):
        return exc
    if isinstance(exc, VPSSSHConnectionError):
        return SSHTransportError(exc.code, exc.message)
    if isinstance(exc, HostTrustError):
        return SSHTransportError(exc.code, exc.message)
    if trust is not None and trust.error is not None:
        return SSHTransportError(trust.error.code, trust.error.message)
    kind = type(exc).__name__
    if kind in {"PermissionDenied", "PasswordChangeRequired"}:
        return SSHTransportError("SSH_AUTH_FAILED", "SSH 登录失败，请核对保存的凭据")
    if kind == "HostKeyNotVerifiable":
        return _classify_host_key_error(exc)
    if kind == "KeyExchangeFailed":
        return SSHTransportError("SSH_PROTOCOL_NEGOTIATION_FAILED", "SSH 协议或密钥交换算法协商失败")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return SSHTransportError("SSH_CONNECT_TIMEOUT", "SSH 连接或认证超时")
    if kind in {"ConnectionLost", "ConnectionResetError", "BrokenPipeError"}:
        return SSHTransportError("SSH_CHANNEL_CLOSED", "SSH 连接已断开")
    return SSHTransportError("SSH_CONNECTION_FAILED", "SSH 连接失败，请检查目标和网络")


class _SSHAuthentication:
    def __init__(self, password, trust, challenge):
        self.secret = password
        self._trust = trust
        self._challenge = challenge
        self.sent_password = False
        self.challenge_rounds = 0
        self.failure = None

    def validate_host_public_key(self, host, address, port, key):
        return self._trust is not None and self._trust.validate_candidate(host, address, port, key)

    async def _confirm_host_trust(self):
        if self._trust is not None:
            await asyncio.to_thread(self._trust.commit_before_authentication)

    async def password_auth_requested(self):
        if self.sent_password:
            return None
        # Called only after key exchange proves host-key possession.
        try:
            await self._confirm_host_trust()
        except HostTrustError as exc:
            self._trust.error = exc
            raise
        self.sent_password = True
        secret, self.secret = self.secret, None
        return secret

    def kbdint_auth_requested(self):
        return "" if not self.sent_password or self._challenge is not None else None

    async def kbdint_challenge_received(self, name, instructions, lang, prompts):
        self.challenge_rounds += 1
        if self.challenge_rounds > 4 or len(prompts) > 8:
            return None
        if not prompts:
            return []
        if (
            not self.sent_password
            and len(prompts) == 1
            and not prompts[0][1]
            and re.search(r"password\s*:\s*$", prompts[0][0], re.I)
        ):
            answer = await self.password_auth_requested()
            return [answer] if answer is not None else None
        if self._challenge is None:
            self.failure = SSHTransportError(
                "SSH_AUTH_CHALLENGE_REQUIRED", "SSH 服务器要求额外认证信息"
            )
            return None
        await self._confirm_host_trust()
        return await self._challenge(name, instructions, prompts)

    def auth_completed(self):
        self.secret = None

    def connection_lost(self, exc):
        self.secret = None


class _InteractiveSession:
    def __init__(self, owner, on_data, on_exit):
        self._owner = owner
        self._on_data = on_data
        self._on_exit = on_exit
        self.channel = None
        self.writable = asyncio.Event()
        self.writable.set()
        self.closed = asyncio.Event()
        self.eof = False
        self.paused = False
        self.exit_status = None
        self.exit_signal = None
        self.expected_close = False

    def connection_made(self, channel):
        self.channel = channel
        self._owner.channel, self._owner.protocol = channel, self
        channel.set_write_buffer_limits(
            high=self._owner.config.pending_input_bytes,
            low=self._owner.config.input_frame_bytes,
        )

    def data_received(self, data, datatype):
        self._on_data(bytes(data))

    def eof_received(self):
        self.eof = True
        return False

    def exit_status_received(self, status):
        self.exit_status = status

    def exit_signal_received(self, signal, core_dumped, message, lang):
        self.exit_signal = signal if isinstance(signal, str) and len(signal) <= 32 else "UNKNOWN"

    def pause_writing(self):
        self.writable.clear()

    def resume_writing(self):
        self.writable.set()

    def connection_lost(self, exc):
        self.closed.set()
        self.writable.set()
        # Closing while read delivery is paused can discard bytes in
        # AsyncSSH, even when the transport reports a clean close.
        complete = (
            exc is None
            and not self.paused
            and (self.eof or self.exit_status is not None or self.exit_signal is not None)
        )
        if not self.expected_close:
            self._on_exit(
                {
                    "exit_status": self.exit_status,
                    "exit_signal": self.exit_signal,
                    "output_complete": complete,
                    "reason_code": "SSH_CHANNEL_CLOSED"
                    if exc is not None
                    else ("EXITED" if complete else "OUTPUT_INCOMPLETE"),
                }
            )


class _ControlCapture:
    def __init__(self, result, max_bytes):
        self._result = result
        self._max_bytes = max_bytes
        self._size = 0
        self._chunks = deque()
        self.channel = None
        self.status = None

    def connection_made(self, channel):
        self.channel = channel

    def data_received(self, data, datatype):
        if self._result.done():
            return
        self._size += len(data)
        if self._size > self._max_bytes:
            self._result.set_exception(
                SSHTransportError("SSH_CONTROL_OUTPUT_LIMIT", "SSH 控制响应超出限制")
            )
            self.channel.abort()
        else:
            self._chunks.append(bytes(data))

    def exit_status_received(self, status):
        self.status = status

    def eof_received(self):
        return False

    def connection_lost(self, exc):
        if self._result.done():
            return
        if exc is not None or self.status is None:
            self._result.set_exception(
                SSHTransportError("SSH_CONTROL_UNCERTAIN", "SSH 控制命令结果尚未确认")
            )
        else:
            self._result.set_result((self.status, b"".join(self._chunks)))


class AsyncSSHTransport:
    """One authenticated connection, with one replaceable interactive channel.

    ``on_data`` and ``on_exit`` are synchronous, bounded callbacks. They only
    append to the owner's ring/set its wake event; they must never await a WS.
    """

    def __init__(self, config, *, ssh_module=None, trust_sources=None, trust_write_path=None):
        self.config = config
        self.ssh = ssh_module
        self.trust_sources, self.trust_write_path = trust_sources, trust_write_path
        self.connection = None
        self.channel = None
        self.protocol = None
        self.trust = None
        self._vps_connection = None
        self.transport_contract = None
        self.target_endpoint = None
        self._closed = False

    @property
    def connected(self):
        return self.connection is not None and not self._closed and not self.connection.is_closed()

    async def _load_host_trust(self, target):
        if target.get("saved_vps_password_only") is True:
            # Trusted saved-VPS Hub operations only, never system/generic SSH.
            return None
        return await asyncio.to_thread(
            HostKeyPolicy,
            self.ssh,
            target.get("host_key_policy", "strict"),
            sources=self.trust_sources,
            write_path=self.trust_write_path,
            expected_fingerprint=target.get("host_key_fingerprint", ""),
        )

    async def _connect_managed_vps(self, target, password):
        try:
            connection = await connect_vps(
                host=target["host"],
                port=target.get("port", 22),
                username=target["username"],
                password=password,
                connect_timeout=self.config.connect_timeout,
                login_timeout=self.config.login_timeout,
                keepalive_interval=self.config.ssh_keepalive_interval,
                keepalive_count_max=self.config.ssh_keepalive_count,
                challenge=None,
                ssh_module=self.ssh,
            )
            self._vps_connection = connection
            self.connection, self.ssh = connection.connection, connection.ssh_module
            self.transport_contract = VPS_SSH_PROTOCOL
            self.host_identity_verification_applied = VPS_HOST_IDENTITY_VERIFICATION
            self.authentication_method = connection.authentication_method
            self.target_endpoint = (target["host"], int(target.get("port", 22)))
            return self
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise classify_error(exc) from None

    async def connect(self, target, password, *, challenge=None):
        if target.get("connection_contract") == VPS_SSH_PROTOCOL:
            return await self._connect_managed_vps(target, password)
        if self.ssh is None:
            try:
                self.ssh = importlib.import_module("asyncssh")
            except ImportError as exc:
                raise SSHTransportError(
                    "SSH_DEPENDENCY_MISSING", "Agent 尚未安装 Web SSH 依赖，请更新 Agent"
                ) from exc
        self.trust = await self._load_host_trust(target)
        ssh, trust = self.ssh, self.trust

        class Client(_SSHAuthentication, ssh.SSHClient):
            pass

        # Do not relay arbitrary OTP/echoed prompts from a host whose identity
        # is not checked. The fixed saved-password KBI prompt still works.
        client = Client(password, trust, challenge if trust is not None else None)
        try:
            self.connection = await asyncio.wait_for(
                ssh.connect(
                    target["host"],
                    port=target.get("port", 22),
                    username=target["username"],
                    client_factory=lambda: client,
                    known_hosts=None if trust is None else trust.known_hosts,
                    config=None,
                    # AsyncSSH treats [] as a request to load default local keys.
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
                    connect_timeout=self.config.connect_timeout,
                    login_timeout=self.config.login_timeout,
                    keepalive_interval=self.config.ssh_keepalive_interval,
                    keepalive_count_max=self.config.ssh_keepalive_count,
                ),
                self.config.connect_timeout + self.config.login_timeout,
            )
            # A server may allow the SSH 'none' authentication method.
            await client._confirm_host_trust()
            return self
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise (client.failure or classify_error(exc, trust)) from None
        finally:
            client.secret = None

    async def open_pty(self, cols, rows, on_data, on_exit, *, command=None):
        if self.connection is None or self._closed:
            raise SSHTransportError("SSH_CHANNEL_CLOSED", "SSH 连接已关闭")
        await self.close_channel()
        ssh = self.ssh

        class Session(_InteractiveSession, ssh.SSHClientSession):
            pass

        protocol = Session(self, on_data, on_exit)
        try:
            channel, _ = await asyncio.wait_for(
                self.connection.create_session(
                    lambda: protocol,
                    command=command,
                    request_pty="force",
                    term_type="xterm-256color",
                    term_size=(cols, rows),
                    encoding=None,
                    window=self.config.ssh_window_bytes,
                    max_pktsize=self.config.frame_bytes,
                    x11_forwarding=False,
                ),
                self.config.login_timeout,
            )
        except BaseException:
            protocol.expected_close = True
            if protocol.channel is not None:
                protocol.channel.close()
            raise
        self.channel, self.protocol = channel, protocol

    def write(self, data: bytes) -> None:
        if self.channel is None or self.protocol.closed.is_set():
            raise SSHTransportError("SSH_CHANNEL_CLOSED", "SSH 通道已关闭")
        if self.channel.get_write_buffer_size() + len(data) > self.config.pending_input_bytes:
            raise SSHTransportError("INPUT_BACKPRESSURE", "输入缓冲已满，请等待 SSH 接收")
        self.channel.write(data)

    async def wait_writable(self):
        protocol = self.protocol
        if protocol is None:
            raise SSHTransportError("SSH_CHANNEL_CLOSED", "SSH 通道已关闭")
        await asyncio.wait_for(protocol.writable.wait(), self.config.write_timeout)
        if protocol.closed.is_set():
            raise SSHTransportError("SSH_CHANNEL_CLOSED", "SSH 通道已关闭")

    def resize(self, cols, rows):
        if self.channel is None:
            raise SSHTransportError("SSH_CHANNEL_CLOSED", "SSH 通道已关闭")
        self.channel.change_terminal_size(cols, rows)

    def pause_output(self):
        if self.channel is not None and not self.protocol.paused:
            self.protocol.paused = True
            self.channel.pause_reading()

    def resume_output(self):
        if self.channel is not None and self.protocol.paused:
            self.protocol.paused = False
            self.channel.resume_reading()

    async def close_channel(self):
        channel, protocol = self.channel, self.protocol
        self.channel = self.protocol = None
        if channel is None:
            return
        protocol.expected_close = True
        channel.close()
        try:
            await asyncio.wait_for(channel.wait_closed(), self.config.shutdown_timeout)
        except (TimeoutError, ConnectionError):
            channel.abort()

    async def close(self):
        self._closed = True
        await self.close_channel()
        connection, self.connection = self.connection, None
        vps_connection, self._vps_connection = self._vps_connection, None
        if vps_connection is not None:
            await vps_connection.close(timeout=self.config.shutdown_timeout)
        elif connection is not None:
            connection.close()
            try:
                await asyncio.wait_for(connection.wait_closed(), self.config.shutdown_timeout)
            except (TimeoutError, ConnectionError):
                connection.abort()

    async def run_control(self, command, *, wait_seconds=None, max_bytes=8192):
        """Run a fixed tmux control command with bounded combined output."""
        if self.connection is None:
            raise SSHTransportError("SSH_CHANNEL_CLOSED", "SSH 连接已关闭")
        result = asyncio.get_running_loop().create_future()
        result.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

        class Capture(_ControlCapture, self.ssh.SSHClientSession):
            pass

        protocol = Capture(result, max_bytes)
        channel = None
        try:
            async with asyncio.timeout(wait_seconds or self.config.login_timeout):
                channel, _ = await self.connection.create_session(
                    lambda: protocol,
                    command=command,
                    request_pty=False,
                    encoding=None,
                    window=min(max_bytes, self.config.ssh_window_bytes),
                    max_pktsize=min(max_bytes, self.config.frame_bytes),
                )
                return await result
        except TimeoutError as exc:
            raise SSHTransportError(
                "SSH_CONTROL_UNCERTAIN", "SSH 控制命令超时，不能确定远端结果"
            ) from exc
        finally:
            if channel is not None:
                channel.close()
            elif protocol.channel is not None:
                protocol.channel.close()
            if not result.done():
                result.cancel()
