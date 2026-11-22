"""Real loopback SSH + OS PTY + isolated tmux persistence acceptance.

Run from the project root with an explicit verified tmux fixture:
  CODEPIER_TEST_TMUX="$PWD/.work/web-ssh-tools/bin/tmux" PYTHONPATH="$PWD" \
    .venv/bin/python -m pytest -q -s tests/test_ssh_terminal_tmux_real.py

If CODEPIER_TEST_TMUX is unset, an existing tmux is located with shutil.which.
The parent process's PATH, TMPDIR, and shell profile settings aren't modified.

Only synthetic loopback credentials, a temporary HOME/known_hosts, and random
CodePier tmux sockets are used. No user tmux socket or configuration is touched.
This proves the Agent/SSH/PTY/tmux boundary, not browser rendering or production.
Python's forkpty deprecation warning may be emitted by this isolated fixture:
only the synthetic SSH server forks a local OS PTY. The product opens the remote
PTY through AsyncSSH and does not fork a local terminal. Warnings stay visible.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import errno
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import tempfile
import uuid

import pytest

from agent.ssh_terminal_manager import TerminalSessionManager
from agent.ssh_terminal_tmux import TmuxReference
from agent.ssh_terminal_transport import AsyncSSHTransport
from shared.ssh_terminal_config import DEFAULT_CONFIG
from shared.ssh_terminal_protocol import FrameKind, TerminalFrame


async def until(predicate, seconds=15):
    async with asyncio.timeout(seconds):
        while not predicate():  # noqa: ASYNC110 -- bounded observation of the real manager/OS fixture
            await asyncio.sleep(0.01)


class RealPTYSession:
    """A tiny fixture sshd: PTY exec really runs a local controlling terminal."""

    def __init__(self, env, directory, owner):
        self.env, self.directory, self.owner = env, directory, owner
        self.channel = self.master = self.pid = self.task = self.process = None
        self.command = None
        self.pty = self.closed = self.paused = False
        self.size = (120, 30)
        owner.add(self)

    def connection_made(self, channel):
        self.channel = channel

    def pty_requested(self, term, size, modes):
        self.pty, self.size = True, size[:2]
        self.env = {**self.env, "TERM": term or "xterm-256color"}
        return True

    def terminal_size_changed(self, cols, rows, *rest):
        self.size = cols, rows
        if self.master is not None:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def exec_requested(self, command):
        self.command = command
        return len(command) <= 8192

    def shell_requested(self):
        self.command = "/bin/sh"
        return True

    def session_started(self):
        if self.pty:
            import pty
            # forkpty gives the child its own session and controlling terminal.
            # The child immediately chdirs/execs; it runs no event-loop work.
            child, master = pty.fork()
            if child == 0:
                try:
                    os.chdir(self.directory)
                    os.execve("/bin/sh", ["sh", "-c", self.command], self.env)
                finally:
                    os._exit(127)
            self.pid, self.master = child, master
            os.set_blocking(master, False)
            self.terminal_size_changed(*self.size)
            asyncio.get_running_loop().add_reader(master, self._read)
            self.task = asyncio.create_task(self._wait_child())
        else:
            self.task = asyncio.create_task(self._control())

    async def _control(self):
        try:
            self.process = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", self.command, env=self.env, cwd=self.directory,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            output, _ = await asyncio.wait_for(self.process.communicate(), 10)
            assert len(output) <= 65536, "Fixture control output exceeded its budget"
            if not self.closed:
                self.channel.write(output)
                self.channel.exit(self.process.returncode)
        finally:
            if self.process and self.process.returncode is None:
                self.process.kill()
                await self.process.wait()

    def _read(self):
        if self.master is None or self.closed:
            return
        try:
            data = os.read(self.master, 32768)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if data:
            self.channel.write(data)
        else:
            self._close_master()

    async def _wait_child(self):
        while self.pid is not None:
            child, status = os.waitpid(self.pid, os.WNOHANG)  # noqa: ASYNC222 -- WNOHANG never blocks
            if child:
                self.pid = None
                self._read()
                self._close_master()
                if not self.closed:
                    self.channel.exit(max(0, os.waitstatus_to_exitcode(status)))
                return
            await asyncio.sleep(0.01)

    def data_received(self, data, datatype):
        if self.master is not None and not self.closed:
            assert len(data) <= 4096, "Fixture sends only small synthetic commands"
            assert os.write(self.master, data) == len(data)

    def pause_writing(self):
        self.paused = True
        if self.master is not None:
            asyncio.get_running_loop().remove_reader(self.master)

    def resume_writing(self):
        self.paused = False
        if self.master is not None and not self.closed:
            asyncio.get_running_loop().add_reader(self.master, self._read)

    def _close_master(self):
        if self.master is not None:
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)
            self.master = None

    def connection_lost(self, exc):
        self.closed = True
        self._close_master()
        if self.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGHUP)
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()

    def eof_received(self):
        return False


class TerminalDriver:
    def __init__(self, config, known_hosts, scope):
        self.output = bytearray()
        self.scope = scope
        self.manager = TerminalSessionManager(
            emit=self.emit, config=config, authorize=lambda _: None,
            connector=lambda: AsyncSSHTransport(config, trust_sources=[known_hosts]))

    async def emit(self, event):
        if event["type"] != "terminal_output":
            return
        data = event["payload"]
        self.output.extend(data)
        assert len(self.output) < 1024 * 1024, "Unexpected fixture output growth"
        await self.manager.acknowledge({
            "session_id": event["session_id"], "runtime_generation": self.manager.generation,
            "attachment_id": event["attachment_id"], "render_epoch": event["render_epoch"],
            "stream_epoch": event["stream_epoch"], "rendered_offset": str(int(event["offset"]) + len(data)),
        })

    async def create(self, request, *, recover=False):
        request = {**request, "runtime_generation": self.manager.generation, "recover": recover}
        grant = await self.manager.control("reserve_create", request, self.scope)
        await self.manager.control("create", {**request, "admission_token": grant["admission_token"]}, self.scope)
        await self.settled(request["session_id"])
        return request

    async def settled(self, sid):
        session = self.manager.sessions[sid]
        if session.start_task:
            await asyncio.wait_for(asyncio.shield(session.start_task), 20)
        assert session.state in {"ready", "detached"}, (session.state, session.reason)
        assert session.mode == "tmux" and session.transport.connected

    async def attach(self, request):
        session = self.manager.sessions[request["session_id"]]
        binding = {**request, "attachment_id": uuid.uuid4().hex, "renderer_id": uuid.uuid4().hex,
                   "render_epoch": str(session.render_epoch + 1), "writer_epoch": str(session.writer_epoch + 1),
                   "stream_epoch": str(session.stream_epoch), "rendered_offset": "0"}
        await self.manager.control("attach", binding, self.scope)
        return binding

    async def check_shell(self, request, expected=None, *, initialize=None):
        session = self.manager.sessions[request["session_id"]]
        label = "PROBE_" + uuid.uuid4().hex[:12]
        command = ("CP_WEBSSH_TOKEN=" + initialize + "; " if initialize else "")
        command += "printf '\\n" + label + ":%s:%s\\n' \"$CP_WEBSSH_TOKEN\" \"$$\"\r"
        frame = TerminalFrame(FrameKind.INPUT, session.id, self.manager.generation,
                              session.stream_epoch, session.writer_epoch, session.tracker.next_input_seq,
                              command.encode())
        assert (await self.manager.input(frame))["status"] == "forwarded"
        # A fresh random label prevents old redraw/history from proving recovery.
        pattern = re.compile(label.encode() + rb":([0-9a-f]{32}):([0-9]+)")
        await until(lambda: pattern.search(self.output) is not None)
        match = pattern.search(self.output)
        identity = match[1].decode(), int(match[2])
        if expected is not None:
            assert identity == expected, "Recovery changed the original shell variable or PID"
        return identity


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.serial_regression
async def test_real_ssh_pty_tmux_same_shell_survives_disconnect_and_agent_restart(tmp_path):
    if os.name != "posix":
        pytest.skip("A POSIX OS PTY and real tmux are required")
    binary = os.environ.get("CODEPIER_TEST_TMUX") or shutil.which("tmux")
    if not binary:
        pytest.skip("Set CODEPIER_TEST_TMUX to a verified tmux binary, or install tmux")
    ssh = pytest.importorskip("asyncssh")
    with tempfile.TemporaryDirectory(prefix="cpssh-", dir="/tmp") as short:
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.bind(short + "/probe")
                probe.listen()
            master, slave = os.openpty()
            os.close(master)
            os.close(slave)
        except PermissionError:
            pytest.skip("Execution environment denies the Unix socket/OS PTY needed by tmux")
        home = Path(short) / "home"
        home.mkdir(mode=0o700)
        env = {**os.environ, "PATH": str(Path(binary).parent) + os.pathsep + os.environ.get("PATH", ""),
               "TMUX_TMPDIR": short, "HOME": str(home), "SHELL": "/bin/sh", "ENV": "/dev/null",
               "BASH_ENV": "/dev/null", "ZDOTDIR": str(home), "TERM": "xterm-256color"}
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        sessions, connections = set(), set()
        password = "synthetic-" + uuid.uuid4().hex

        class PeerSession(RealPTYSession, ssh.SSHServerSession):
            pass

        class Peer(ssh.SSHServer):
            def connection_made(self, connection):
                connections.add(connection)

            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, supplied):
                return username == "fixture" and supplied == password

            def session_requested(self):
                return PeerSession(env, short, sessions)

        server = await ssh.create_server(Peer, "127.0.0.1", 0, encoding=None, line_editor=False,
                                         server_host_keys=[ssh.generate_private_key("ssh-ed25519")])
        config = replace(DEFAULT_CONFIG, lease_seconds=120.0)
        scope = {"id": "fixture-project", "root": str(tmp_path), "mode": "write", "allow_tasks": True,
                 "_terminal_owner": "user:fixture-owner", "_terminal_space": "fixture-space"}
        sid = uuid.uuid4().hex
        reference = TmuxReference.for_session(sid)
        request = {"session_id": sid, "owner_user_id": "fixture-owner", "space_id": "fixture-space",
                   "vps_id": "fixture-vps", "connection_revision": 1, "binding_id": "fixture-binding",
                   "authority_revision": uuid.uuid4().hex, "host": "127.0.0.1", "port": server.get_port(),
                   "username": "fixture", "password": password, "host_key_policy": "accept-new",
                   "mode": "tmux", "cols": 120, "rows": 30}
        drivers = []

        async def tmux(*arguments):
            process = await asyncio.create_subprocess_exec(
                binary, "-L", reference.socket, "-f", "/dev/null", *arguments,
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            output, _ = await asyncio.wait_for(process.communicate(), 5)
            return process.returncode, output

        try:
            first = TerminalDriver(config, tmp_path / "known_hosts", scope)
            drivers.append(first)
            request = await first.create(request)
            attached = await first.attach(request)
            identity = await first.check_shell(request, initialize=uuid.uuid4().hex)
            pane_pid = (await tmux("display-message", "-p", "-t", reference.session, "#{pane_pid}"))[1].strip()
            assert pane_pid == str(identity[1]).encode()
            await first.manager.control("detach", attached, scope)
            await first.attach(request)
            await first.check_shell(request, identity)
            live = first.manager.sessions[sid]
            live.transport.connection.abort()  # Actual SSH TCP connection failure.
            await until(lambda: not live.transport.connected and live.ended is not None)
            request["authority_revision"] = uuid.uuid4().hex
            await first.manager.control("recover", request, scope)
            await first.settled(sid)
            await first.attach(request)
            await first.check_shell(request, identity)
            await first.manager.close_all()  # Agent shutdown closes SSH, preserves tmux.
            assert (await tmux("has-session", "-t", reference.session))[0] == 0
            second = TerminalDriver(config, tmp_path / "known_hosts", scope)
            drivers.append(second)
            request["authority_revision"] = uuid.uuid4().hex
            request = await second.create(request, recover=True)
            assert second.manager.generation != first.manager.generation
            await second.attach(request)
            await second.check_shell(request, identity)
            await second.manager.control("close", request, scope)
            assert (await tmux("has-session", "-t", reference.session))[0] != 0
            third = TerminalDriver(config, tmp_path / "known_hosts", scope)
            drivers.append(third)
            missing_request = {**request, "runtime_generation": third.manager.generation,
                               "authority_revision": uuid.uuid4().hex, "recover": True}
            grant = await third.manager.control("reserve_create", missing_request, scope)
            await third.manager.control("create", {**missing_request, "admission_token": grant["admission_token"]}, scope)
            missing = third.manager.sessions[sid]
            if missing.start_task:
                await asyncio.wait_for(asyncio.shield(missing.start_task), 20)
            assert missing.state == "failed" and missing.reason == "REMOTE_SESSION_MISSING"
            assert (await tmux("has-session", "-t", reference.session))[0] != 0
            print("REAL_TMUX_ACCEPTED: same logical ID, variable and shell PID across redraw, SSH failure and Agent restart; explicit close and missing recovery verified")
        finally:
            await asyncio.gather(*(driver.manager.close_all() for driver in drivers), return_exceptions=True)
            # This is only the test's random dedicated server, never the default.
            await tmux("kill-server")
            for connection in connections:
                connection.abort()
            server.close()
            await server.wait_closed()
            for session in sessions:
                session.connection_lost(None)
            pending = [session.task for session in sessions if session.task and not session.task.done()]
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
