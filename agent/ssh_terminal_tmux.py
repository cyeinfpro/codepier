"""Fixed tmux commands scoped to one generated, CodePier-owned session."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
import uuid

from .ssh_terminal_transport import SSHTransportError


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

    def prefix(self):
        if not re.fullmatch(r"codepier_[0-9a-f]{32}", self.socket) or not re.fullmatch(r"cp_[0-9a-f]{32}", self.session):
            raise SSHTransportError("INVALID_TMUX_REFERENCE", "持久会话引用无效")
        return "tmux -u -L " + shlex.quote(self.socket) + " -f /dev/null"

    def public(self):
        return {"socket": self.socket, "session": self.session}


class TmuxController:
    def __init__(self, transport, session_id, config):
        self.transport, self.config = transport, config
        self.reference = TmuxReference.for_session(session_id)

    async def probe(self):
        status, output = await self.transport.run_control(
            "if command -v tmux >/dev/null 2>&1; then tmux -V; else exit 127; fi",
            max_bytes=1024,
        )
        if status == 127:
            return {"available": False, "reason": "TMUX_NOT_INSTALLED"}
        if status != 0:
            raise SSHTransportError("PERSISTENCE_PROBE_FAILED", "无法确认 tmux 能力")
        match = re.fullmatch(rb"tmux (\d+)\.(\d+)[a-z]?(?:[^\r\n]*)\r?\n?", output.strip())
        if not match:
            raise SSHTransportError("PERSISTENCE_PROBE_FAILED", "tmux 版本响应无法核验")
        version = (int(match[1]), int(match[2]))
        if version < (2, 6):
            return {"available": False, "reason": "TMUX_VERSION_UNSUPPORTED"}
        return {"available": True, "version": f"{version[0]}.{version[1]}"}

    async def exists(self):
        status, output = await self.transport.run_control(
            self.reference.prefix() + " has-session -t " + shlex.quote(self.reference.session),
            max_bytes=4096,
        )
        if status == 0:
            return True
        missing = any(marker in output.lower() for marker in (
            b"no such file or directory", b"no server running", b"can't find session",
            b"cannot find session", b"session not found",
        ))
        if status == 1 and missing:
            return False
        raise SSHTransportError("PERSISTENCE_PROBE_FAILED", "无法确认持久会话状态")

    async def create(self, cols, rows):
        if await self.exists():
            raise SSHTransportError("REMOTE_SESSION_EXISTS", "该持久会话已经存在，请恢复原会话")
        command = (
            self.reference.prefix() + " start-server"
            + " \\; set-option -g history-limit " + str(self.config.tmux_history_lines)
            + " \\; new-session -d -s " + shlex.quote(self.reference.session)
            + f" -x {int(cols)} -y {int(rows)}"
            + " \\; set-option -g destroy-unattached off"
            + " \\; set-option -s exit-empty on"
        )
        status, _ = await self.transport.run_control(command)
        if status != 0:
            # It may have created the session before a later option failed.
            # Caller must query/recover this exact ID, never fall back to a new
            # direct shell after an uncertain remote side effect.
            raise SSHTransportError("SSH_SESSION_UNCERTAIN", "持久会话创建结果不完整，请查询原会话")
        if not await self.exists():
            raise SSHTransportError("REMOTE_SESSION_MISSING", "持久会话创建后已退出")
        return self.reference.public()

    async def recover(self):
        if not await self.exists():
            raise SSHTransportError("REMOTE_SESSION_MISSING", "原持久会话已不存在")
        return self.reference.public()

    def attach_command(self):
        # -d only detaches clients of this randomly named dedicated session.
        # There is deliberately no `new-session -A` recovery path.
        return self.reference.prefix() + " attach-session -d -t " + shlex.quote(self.reference.session)

    async def close(self):
        if not await self.exists():
            return {"ended": True, "already_missing": True}
        status, _ = await self.transport.run_control(
            self.reference.prefix() + " kill-session -t " + shlex.quote(self.reference.session)
        )
        if status != 0 and await self.exists():
            raise SSHTransportError("SSH_CONTROL_UNCERTAIN", "尚未确认持久会话结束")
        return {"ended": True}
