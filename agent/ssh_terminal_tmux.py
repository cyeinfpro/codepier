"""Scoped tmux controls and a separate authorized VPS environment provisioner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from inspect import isawaitable
import re
import shlex
import time
import uuid
from weakref import WeakValueDictionary

from shared.ssh_host_pin import normalize_host_pin
from shared.vps_transport import VPS_SSH_PROTOCOL
from shared.ssh_terminal_config import TMUX_PROVISION_TIMEOUT_SECONDS

from .ssh_terminal_transport import SSHTransportError


_TMUX_PATHS = ("/usr/bin/tmux", "/bin/tmux", "/usr/local/bin/tmux", "/opt/homebrew/bin/tmux")
# This token represents exactly one verified per-login macOS path, never a
# path supplied by remote output or a binary discovered through arbitrary PATH.
_USER_TMUX_TOKEN = "$HOME/.local/bin/tmux"
_USER_TMUX_BINARY = '"$HOME/.local/bin/tmux"'
_USER_TMUX_CHECK = (
    'test "$(/usr/bin/uname -s)" = Darwin && test -n "${HOME:-}" '
    '&& test ! -L "$HOME/.local" && test -O "$HOME/.local" '
    '&& test ! -L "$HOME/.local/bin" && test -O "$HOME/.local/bin" '
    '&& test ! -L "$HOME/.local/bin/tmux" && test -O "$HOME/.local/bin/tmux" '
    '&& test -f "$HOME/.local/bin/tmux" && test -x "$HOME/.local/bin/tmux"'
)
_PROBE = (
    "located=$(command -v tmux 2>/dev/null) || located=; "
    + 'for candidate in "$located" '
    + " ".join(shlex.quote(path) for path in _TMUX_PATHS)
    + "; do "
    + 'case "$candidate" in '
    + "|".join(_TMUX_PATHS)
    + ") ;; *) continue ;; esac; "
    + 'if test -f "$candidate" && test -x "$candidate"; then '
    + 'printf \'%s\\n\' "$candidate"; exec "$candidate" -V; fi; done; '
    + f"if {_USER_TMUX_CHECK}; then "
    + "printf '%s\n' "
    + shlex.quote(_USER_TMUX_TOKEN)
    + "; "
    + f"exec {_USER_TMUX_BINARY} -V; fi; "
    # A PATH-visible but unverified executable/function is not absence. Never
    # execute it or start a package transaction on that evidence.
    + 'test -z "$located" || exit 78; exit 127'
)
_FAILURE_COOLDOWN_SECONDS = 30
_MAX_UNCERTAIN_ENDPOINTS = 256
_HOLDS = WeakValueDictionary()
# Uncertain outcomes are never auto-replayed. Keep them until a probe proves
# tmux available; refuse further writes rather than evicting unresolved entries.
_UNCERTAIN = set()
_RECENT_FAILURES = {}
_INSTALL = r"""
system=$(/usr/bin/uname -s) || exit 70
uid=$(/usr/bin/id -u) || exit 70
case "$system" in
Linux)
    test -f /etc/debian_version && test -x /usr/bin/apt-get || exit 69
    set -- /usr/bin/apt-get -y --no-install-recommends \
        -o Acquire::Retries=2 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 \
        -o DPkg::Lock::Timeout=45 -o Dpkg::Use-Pty=0 install tmux
    if test "$uid" = 0; then
        DEBIAN_FRONTEND=noninteractive "$@" </dev/null >/dev/null 2>&1 || exit 70
    else
        test -x /usr/bin/sudo || exit 77
        /usr/bin/sudo -n -l -- "$@" </dev/null >/dev/null 2>&1 || exit 77
        /usr/bin/sudo -n -k -- "$@" </dev/null >/dev/null 2>&1 || exit 70
    fi
    ;;
Darwin)
    test "$uid" != 0 || exit 77
    for brew in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        test -f "$brew" && test -x "$brew" || continue
        test -O "${brew%/bin/brew}" || exit 77
        HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
            "$brew" install tmux </dev/null >/dev/null 2>&1 || exit 70
        exit 0
    done
    exit 69
    ;;
*) exit 69 ;;
esac
exit 0
"""
_INSTALL_FAILURES = {
    69: ("TMUX_INSTALL_UNAVAILABLE", "目标机没有受支持的既有包管理器，无法自动安装 tmux"),
    77: ("TMUX_INSTALL_PERMISSION", "当前 SSH 用户没有非交互式 tmux 安装权限"),
}
_INSTALL_FAILED = ("TMUX_INSTALL_FAILED", "自动安装 tmux 失败，请检查目标机的包管理器")
_INSTALL_UNCERTAIN = ("TMUX_INSTALL_UNCERTAIN", "tmux 安装结果尚未确认，不会自动重试")


async def _probe_tmux(transport):
    status, output = await transport.run_control(_PROBE, max_bytes=1024)
    if status == 127 and not output.strip():
        return {"available": False, "reason": "TMUX_NOT_INSTALLED"}, None
    if status == 78 and not output.strip():
        return {"available": False, "reason": "TMUX_LOCATION_UNSUPPORTED"}, None
    if status != 0:
        raise SSHTransportError("PERSISTENCE_PROBE_FAILED", "无法确认 tmux 能力")
    fields = output.strip().splitlines()
    match = (
        re.fullmatch(rb"tmux (\d+)\.(\d+)[a-z]?(?:[^\r\n]*)", fields[1])
        if len(fields) == 2
        and fields[0] in tuple(x.encode() for x in (*_TMUX_PATHS, _USER_TMUX_TOKEN))
        else None
    )
    if not match:
        raise SSHTransportError("PERSISTENCE_PROBE_FAILED", "tmux 路径或版本响应无法核验")
    version = (int(match[1]), int(match[2]))
    binary = fields[0].decode("ascii")
    if version < (2, 6):
        return {"available": False, "reason": "TMUX_VERSION_UNSUPPORTED"}, binary
    return {"available": True, "version": f"{version[0]}.{version[1]}"}, binary


def _normalize_endpoint(host, port):
    return host.strip().strip("[]").rstrip(".").casefold(), port


def _verified_endpoint(transport):
    identity = getattr(transport, "verified_tmux_endpoint", None)
    if not isinstance(identity, tuple) or len(identity) != 3:
        return None
    host, port, fingerprint = identity
    if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
        return None
    try:
        return (*_normalize_endpoint(host, port), normalize_host_pin(fingerprint))
    except (TypeError, ValueError):
        return None


def _vps_bound_endpoint(transport):
    if getattr(transport, "transport_contract", None) != VPS_SSH_PROTOCOL:
        return None
    target = getattr(transport, "target_endpoint", None)
    if not isinstance(target, tuple) or len(target) != 2:
        return None
    host, port = target
    if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
        return None
    return _normalize_endpoint(host, port)


def _endpoint_lock(endpoint):
    hold = _HOLDS.get(endpoint)
    if hold is None:
        hold = _HOLDS[endpoint] = asyncio.Lock()
    return hold


async def probe_tmux(transport):
    """Probe only; a ready result may reconcile only the exact verified peer."""
    identity = _verified_endpoint(transport) or _vps_bound_endpoint(transport)
    if identity is None:
        capability, _binary = await _probe_tmux(transport)
        return capability
    # A ready probe must not clear a fence concurrently with an install that
    # started after the probe's SSH command.
    async with _endpoint_lock(identity[:2]):
        capability, _binary = await _probe_tmux(transport)
        if capability["available"]:
            _UNCERTAIN.discard(identity)
            if _vps_bound_endpoint(transport) is not None:
                _RECENT_FAILURES.pop(identity[:2], None)
        return capability


def _check_install_budget(endpoint, identity):
    if identity in _UNCERTAIN or len(_UNCERTAIN) >= _MAX_UNCERTAIN_ENDPOINTS:
        raise SSHTransportError(*_INSTALL_UNCERTAIN)
    now = time.monotonic()
    for key, (expires, _) in list(_RECENT_FAILURES.items()):
        if expires <= now:
            del _RECENT_FAILURES[key]
    recent = _RECENT_FAILURES.get(endpoint)
    if recent:
        raise SSHTransportError(*recent[1])
    if len(_RECENT_FAILURES) >= _MAX_UNCERTAIN_ENDPOINTS:
        raise SSHTransportError(*_INSTALL_FAILED)


async def _authorize_install(authorize):
    decision = authorize()
    if isawaitable(decision):
        if await decision is not True:
            raise SSHTransportError("AUTH_REVOKED", "tmux 安装授权已失效")
    elif decision is False:
        raise SSHTransportError("AUTH_REVOKED", "tmux 安装授权已失效")


async def _run_install(transport, endpoint, identity):
    # Register before the remote request: cancellation or response loss
    # cannot turn an ambiguous mutation into an automatic retry.
    _UNCERTAIN.add(identity)
    try:
        status, _ = await transport.run_control(
            _INSTALL, wait_seconds=TMUX_PROVISION_TIMEOUT_SECONDS, max_bytes=1024
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise SSHTransportError(*_INSTALL_UNCERTAIN) from exc
    if status not in (0, 69, 70, 77):
        raise SSHTransportError(*_INSTALL_UNCERTAIN)
    if status == 70:
        # The manager may have modified the host before reporting error.
        raise SSHTransportError(*_INSTALL_FAILED)
    if status != 0:
        _UNCERTAIN.discard(identity)
        failure = _INSTALL_FAILURES.get(status, _INSTALL_FAILED)
        _RECENT_FAILURES[endpoint] = (time.monotonic() + _FAILURE_COOLDOWN_SECONDS, failure)
        raise SSHTransportError(*failure)


async def _confirm_install(transport, identity):
    try:
        capability, _binary = await _probe_tmux(transport)
    except Exception as exc:
        raise SSHTransportError(*_INSTALL_UNCERTAIN) from exc
    if not capability["available"]:
        raise SSHTransportError(*_INSTALL_UNCERTAIN)
    _UNCERTAIN.discard(identity)
    return capability


async def provision_tmux(transport, endpoint_key, *, authorize):
    """Check/provision a target through an authorized SSH control connection, no PTY."""
    host, port, _username = endpoint_key
    endpoint = _normalize_endpoint(host, port)
    verified = _verified_endpoint(transport)
    identity = verified if verified is not None and verified[:2] == endpoint else endpoint
    # Lock across SSH users and host keys of the same machine, not per browser/session.
    hold = _endpoint_lock(endpoint)
    async with hold:
        capability, _binary = await _probe_tmux(transport)
        if capability["available"]:
            _UNCERTAIN.discard(identity)
            _RECENT_FAILURES.pop(endpoint, None)
            return capability
        if capability["reason"] != "TMUX_NOT_INSTALLED":
            return capability
        _check_install_budget(endpoint, identity)
        await _authorize_install(authorize)
        await _run_install(transport, endpoint, identity)
        return await _confirm_install(transport, identity)


@dataclass(frozen=True)
class TmuxReference:
    socket: str
    session: str

    @classmethod
    def for_session(cls, session_id: str):
        try:
            ident = uuid.UUID(session_id).hex
        except (ValueError, AttributeError, TypeError) as exc:
            raise SSHTransportError("INVALID_SESSION", "终端会话编号无效") from exc
        return cls("codepier_" + ident, "cp_" + ident)

    def prefix(self, binary="tmux"):
        if not re.fullmatch(r"codepier_[0-9a-f]{32}", self.socket) or not re.fullmatch(
            r"cp_[0-9a-f]{32}", self.session
        ):
            raise SSHTransportError("INVALID_TMUX_REFERENCE", "持久会话引用无效")
        executable = (
            f"{_USER_TMUX_CHECK} && {_USER_TMUX_BINARY}"
            if binary == _USER_TMUX_TOKEN
            else shlex.quote(binary)
        )
        return executable + " -u -L " + shlex.quote(self.socket) + " -f /dev/null"

    def public(self):
        return {"socket": self.socket, "session": self.session}


def _drop_incomplete_utf8_tail(data: bytes) -> bytes:
    for back in range(1, min(4, len(data)) + 1):
        lead = data[-back]
        if 0x80 <= lead < 0xC0:
            continue
        if lead < 0x80:
            return data
        needed = 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4 if lead < 0xF8 else 5
        return data[:-back] if needed > back else data
    return b"" if len(data) < 4 else data


def trim_utf8_scrollback(raw: bytes, max_bytes: int) -> bytes:
    if not raw or max_bytes <= 0:
        return b""
    data = bytes(raw)
    if len(data) > max_bytes:
        data = data[-max_bytes:]
        while data and 0x80 <= data[0] < 0xC0:
            data = data[1:]
    return _drop_incomplete_utf8_tail(data)


class TmuxController:
    def __init__(self, transport, session_id, config):
        self.transport, self.config = transport, config
        self.reference = TmuxReference.for_session(session_id)
        self._binary = None
        self._version = None

    async def probe(self):
        capability, self._binary = await _probe_tmux(self.transport)
        version = capability.get("version")
        self._version = tuple(map(int, version.split("."))) if version else None
        return capability

    def _prefix(self):
        if not self._binary:
            raise SSHTransportError("PERSISTENCE_PROBE_FAILED", "尚未核验 tmux 可执行文件")
        return self.reference.prefix(self._binary)

    async def exists(self):
        if self._binary is None:
            await self.probe()
        status, output = await self.transport.run_control(
            self._prefix() + " has-session -t " + shlex.quote(self.reference.session),
            max_bytes=4096,
        )
        if status == 0:
            return True
        missing = any(
            marker in output.lower()
            for marker in (
                b"no such file or directory",
                b"no server running",
                b"can't find session",
                b"cannot find session",
                b"session not found",
            )
        )
        if status == 1 and missing:
            return False
        raise SSHTransportError("PERSISTENCE_PROBE_FAILED", "无法确认持久会话状态")

    async def create(self, cols, rows):
        if await self.exists():
            raise SSHTransportError("REMOTE_SESSION_EXISTS", "该持久会话已经存在，请恢复原会话")
        window_sizing = (
            "set-window-option -g aggressive-resize on"
            if self._version is not None and self._version < (2, 9)
            else "set-window-option -g window-size latest"
        )
        command = (
            self._prefix()
            + " start-server"
            + " \\; set-option -sg escape-time 10"
            + " \\; set-option -g history-limit "
            + str(self.config.tmux_history_lines)
            + " \\; set-option -g status off"
            + " \\; set-option -g mouse on"
            + " \\; "
            + window_sizing
            + " \\; new-session -d -s "
            + shlex.quote(self.reference.session)
            + f" -x {int(cols)} -y {int(rows)}"
            + " \\; set-option -g destroy-unattached off"
            + " \\; set-option -s exit-empty on"
        )
        status, _ = await self.transport.run_control(command)
        if status != 0:
            # It may have created the session before a later option failed.
            # Caller must query/recover this exact ID, never open another shell.
            raise SSHTransportError("SSH_SESSION_UNCERTAIN", "持久会话创建结果不完整，请查询原会话")
        if not await self.exists():
            raise SSHTransportError("REMOTE_SESSION_MISSING", "持久会话创建后已退出")
        return self.reference.public()

    async def capture_scrollback(self, lines=200, max_bytes=131072):
        if lines <= 0 or max_bytes <= 0:
            return b""
        bounded_lines = min(2000, int(lines))
        bounded_bytes = min(131072, int(max_bytes))
        try:
            if self._binary is None:
                await self.probe()
            command = (
                self._prefix()
                + f" capture-pane -e -p -S -{bounded_lines} -t "
                + shlex.quote(self.reference.session)
            )
            status, output = await self.transport.run_control(command, max_bytes=bounded_bytes)
        except asyncio.CancelledError:
            raise
        except Exception:
            return b""
        if status != 0 or not output:
            return b""
        return trim_utf8_scrollback(output, bounded_bytes)

    async def recover(self):
        if not await self.exists():
            raise SSHTransportError("REMOTE_SESSION_MISSING", "原持久会话已不存在")
        return self.reference.public()

    def attach_command(self):
        # -d only detaches clients of this randomly named dedicated session.
        # There is deliberately no `new-session -A` recovery path.
        return self._prefix() + " attach-session -d -t " + shlex.quote(self.reference.session)

    async def close(self):
        if not await self.exists():
            return {"ended": True, "already_missing": True}
        status, _ = await self.transport.run_control(
            self._prefix() + " kill-session -t " + shlex.quote(self.reference.session)
        )
        if status != 0 and await self.exists():
            raise SSHTransportError("SSH_CONTROL_UNCERTAIN", "尚未确认持久会话结束")
        return {"ended": True}
