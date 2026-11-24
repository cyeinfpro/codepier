"""Real loopback & fault-injection tests for agent/vps_exec.py (vps_ssh_v2 executor).

Covers acceptance matrix:
- T16: Concurrent operations do not mix credentials; concurrent delivery of the same
  operation_id dispatches remote command at most once (remote count == 1).
- T18: Connection drop after remote exec received does not replay command (count == 1,
  remote_execution_state == "remote_outcome_unknown", exit_code is None).
- T19: Crash/failure after dispatch claim before send recovers conservatively as unknown.
- T21: Remote non-zero exit with "Permission denied" in output reports remote failure
  without retrying SSH auth (count == 1, ssh_error is None).
- T22: Post-auth budget exhaustion, hung close on cancel, close failure after confirmed
  terminal exit (no downgrade), signal exit, missing exit status (never exit_code=0).
- T23: Large output bounded tail truncation + cross-chunk password redaction.
"""

from __future__ import annotations

import asyncio
import hashlib
from secrets import token_urlsafe
from typing import Any

import pytest

from agent.vps_exec import ExecDispatchFence, execute_vps_command
from agent.vps_ssh import VPSSSHConnection, VPSSSHConnectionError


def _make_exec_server(ssh, *, users: dict[str, str], handler):
    class Session(ssh.SSHServerSession):
        def __init__(self, username: str):
            self._username = username
            self._chan = None

        def connection_made(self, chan):
            self._chan = chan

        def exec_requested(self, command: str) -> bool:
            asyncio.create_task(handler(self._chan, self._username, command))
            return True

    class Peer(ssh.SSHServer):
        def __init__(self):
            self._username = ""

        def begin_auth(self, username):
            self._username = username
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return users.get(username) == password

        def session_requested(self):
            return Session(self._username)

    return Peer


@pytest.mark.asyncio
async def test_t16_concurrent_operations_isolated_and_same_operation_dispatched_once():
    """T16: Distinct operations stay isolated; duplicate operation_id dispatches only once."""
    ssh = pytest.importorskip("asyncssh")
    pw_a = token_urlsafe(10)
    pw_b = token_urlsafe(10)
    exec_counts: dict[str, int] = {}

    async def handle(chan, username: str, command: str):
        exec_counts[command] = exec_counts.get(command, 0) + 1
        await asyncio.sleep(0.05)
        chan.write(f"user={username} cmd={command}\n".encode())
        chan.exit(0)

    Peer = _make_exec_server(ssh, users={"alice": pw_a, "bob": pw_b}, handler=handle)
    key = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(
        Peer, "127.0.0.1", 0, server_host_keys=[key], encoding=None
    )
    port = listener.get_port()
    fence = ExecDispatchFence()

    try:
        # Concurrent different operations with distinct credentials.
        res_a, res_b = await asyncio.gather(
            execute_vps_command(
                operation_id="op-t16-alice",
                host="127.0.0.1",
                port=port,
                username="alice",
                password=pw_a,
                command="run-alice",
                timeout_seconds=15.0,
                dispatch_fence=fence,
            ),
            execute_vps_command(
                operation_id="op-t16-bob",
                host="127.0.0.1",
                port=port,
                username="bob",
                password=pw_b,
                command="run-bob",
                timeout_seconds=15.0,
                dispatch_fence=fence,
            ),
        )
        assert res_a["exit_code"] == 0 and "user=alice cmd=run-alice" in res_a["output"]
        assert res_b["exit_code"] == 0 and "user=bob cmd=run-bob" in res_b["output"]
        assert res_a["transport_runtime"] == "asyncssh"
        assert res_a["transport_contract"] == "vps_ssh_v2"
        assert res_a["host_identity_verification_applied"] == "disabled"
        assert res_a["authentication_state"] == "accepted"
        assert res_a["authentication_method"] == "password"
        assert res_a["remote_execution_state"] == "completed"

        # Concurrent duplicate delivery of the SAME operation_id -> remote executed ONCE.
        dup_1, dup_2 = await asyncio.gather(
            execute_vps_command(
                operation_id="op-t16-same",
                host="127.0.0.1",
                port=port,
                username="alice",
                password=pw_a,
                command="side-effect-once",
                timeout_seconds=15.0,
                dispatch_fence=fence,
            ),
            execute_vps_command(
                operation_id="op-t16-same",
                host="127.0.0.1",
                port=port,
                username="alice",
                password=pw_a,
                command="side-effect-once",
                timeout_seconds=15.0,
                dispatch_fence=fence,
            ),
        )
        assert exec_counts["side-effect-once"] == 1
        states = sorted([dup_1["remote_execution_state"], dup_2["remote_execution_state"]])
        assert states == ["completed", "remote_outcome_unknown"]
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_t18_disconnect_after_exec_received_never_replays_and_marks_unknown():
    """T18: Dropping the channel after command receipt leaves count == 1 and outcome unknown."""
    ssh = pytest.importorskip("asyncssh")
    pw = token_urlsafe(10)
    exec_count = 0

    async def handle(chan, _username: str, _command: str):
        nonlocal exec_count
        exec_count += 1
        chan.write(b"started-work\n")
        await asyncio.sleep(0.05)
        # Close abruptly without sending exit status or exit signal.
        chan.close()

    Peer = _make_exec_server(ssh, users={"root": pw}, handler=handle)
    key = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(
        Peer, "127.0.0.1", 0, server_host_keys=[key], encoding=None
    )
    try:
        result = await execute_vps_command(
            operation_id="op-t18-drop",
            host="127.0.0.1",
            port=listener.get_port(),
            username="root",
            password=pw,
            command="mutate-remote",
            timeout_seconds=15.0,
            dispatch_fence=ExecDispatchFence(),
        )
        assert exec_count == 1
        assert result["remote_execution_state"] == "remote_outcome_unknown"
        assert result["exit_code"] is None
        assert "started-work" in result["output"]
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_t19_failure_after_dispatch_claim_before_send_recovers_unknown():
    """T19: If dispatch is already claimed before send fails, never replay; report unknown."""
    create_calls = 0

    class FailingSendConn:
        def is_closed(self):
            return False

        def close(self):
            pass

        async def wait_closed(self):
            pass

        async def create_process(self, *_args, **_kwargs):
            nonlocal create_calls
            create_calls += 1
            raise ConnectionResetError("crash before send ack")

    async def fake_connect(**_kwargs):
        return VPSSSHConnection(FailingSendConn(), authentication_method="password")

    fence = ExecDispatchFence()
    result = await execute_vps_command(
        operation_id="op-t19-crash",
        host="127.0.0.1",
        port=22,
        username="root",
        password="pw",
        command="deploy",
        timeout_seconds=10.0,
        dispatch_fence=fence,
        connector=fake_connect,
    )
    assert create_calls == 1
    assert fence.is_claimed("op-t19-crash")
    assert result["remote_execution_state"] == "remote_outcome_unknown"
    assert result["exit_code"] is None


@pytest.mark.asyncio
async def test_t21_remote_nonzero_with_permission_denied_output_not_retried_as_auth():
    """T21: Non-zero remote exit with 'Permission denied' in output is business failure, not SSH auth retry."""
    ssh = pytest.importorskip("asyncssh")
    pw = token_urlsafe(10)
    exec_count = 0

    async def handle(chan, _username: str, _command: str):
        nonlocal exec_count
        exec_count += 1
        chan.write_stderr(b"Permission denied (publickey,password).\n")
        chan.exit(1)

    Peer = _make_exec_server(ssh, users={"root": pw}, handler=handle)
    key = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(
        Peer, "127.0.0.1", 0, server_host_keys=[key], encoding=None
    )
    try:
        result = await execute_vps_command(
            operation_id="op-t21-perm",
            host="127.0.0.1",
            port=listener.get_port(),
            username="root",
            password=pw,
            command="cat /root/protected",
            timeout_seconds=15.0,
            dispatch_fence=ExecDispatchFence(),
        )
        assert exec_count == 1
        assert result["remote_execution_state"] == "completed"
        assert result["exit_code"] == 1
        assert result["ssh_error"] is None
        assert result["authentication_state"] == "accepted"
        assert "Permission denied" in result["output"]
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_t22_budget_exhausted_after_auth_hung_close_terminal_monotonic_signal_and_missing_exit():
    """T22: Post-auth budget exhaustion, hung close, close error after exit, signal exit, missing exit."""

    class FakeStream:
        def __init__(self, chunks: list[bytes]):
            self._chunks = list(chunks)

        async def read(self, _n: int = 65536) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

    class FakeProcess:
        def __init__(self, *, exit_status=None, exit_signal=None, chunks=None, wait_delay=0.0):
            self.stdout = FakeStream(chunks or [])
            self.stderr = None
            self.exit_status = exit_status
            self.exit_signal = exit_signal
            self._wait_delay = wait_delay
            self.terminated = False

        async def wait(self):
            if self._wait_delay > 0:
                await asyncio.sleep(self._wait_delay)

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

        def close(self):
            pass

    # 1) Budget exhausted AFTER auth and BEFORE create_process -> command NOT sent!
    now = [100.0]
    sent_count = 0

    class PostAuthExhaustConn:
        def is_closed(self):
            return False

        def close(self):
            pass

        async def wait_closed(self):
            pass

        async def create_process(self, *_a, **_kw):
            nonlocal sent_count
            sent_count += 1
            return FakeProcess(exit_status=0)

    async def slow_connect(**_kw):
        now[0] += 10.0  # Auth consumed the entire 5s budget.
        return VPSSSHConnection(PostAuthExhaustConn(), authentication_method="password")

    res_exhaust = await execute_vps_command(
        operation_id="op-t22-budget",
        host="127.0.0.1",
        port=22,
        username="root",
        password="pw",
        command="never-send",
        timeout_seconds=5.0,
        clock=lambda: now[0],
        dispatch_fence=ExecDispatchFence(),
        connector=slow_connect,
    )
    assert sent_count == 0
    assert res_exhaust["remote_execution_state"] == "not_started"
    assert res_exhaust["timed_out"] is True
    assert res_exhaust["ssh_error"] == "SSH_TIMEOUT"
    assert res_exhaust["exit_code"] is None

    # 2) Close hangs/fails AFTER confirmed exit_code=0 -> terminal state NOT downgraded!
    class HungCloseConn:
        def __init__(self, process):
            self._process = process
            self.aborted = False

        def is_closed(self):
            return False

        def close(self):
            pass

        async def wait_closed(self):
            await asyncio.sleep(30.0)  # Would hang forever without bounded cleanup!

        def abort(self):
            self.aborted = True

        async def create_process(self, *_a, **_kw):
            return self._process

    hung_conn = HungCloseConn(FakeProcess(exit_status=0, chunks=[b"ok\n"]))

    async def connect_hung(**_kw):
        return VPSSSHConnection(hung_conn, authentication_method="password")

    res_monotonic = await execute_vps_command(
        operation_id="op-t22-monotonic",
        host="127.0.0.1",
        port=22,
        username="root",
        password="pw",
        command="echo ok",
        timeout_seconds=10.0,
        cleanup_timeout=0.15,
        dispatch_fence=ExecDispatchFence(),
        connector=connect_hung,
    )
    assert res_monotonic["remote_execution_state"] == "completed"
    assert res_monotonic["exit_code"] == 0
    assert res_monotonic["ssh_error"] is None
    assert hung_conn.aborted is True
    assert "cleanup_warning" in res_monotonic["ssh_diagnostics"]

    # 3) Signal exit -> completed with exit_signal="TERM" and exit_code=None (never 0).
    sig_conn = HungCloseConn(FakeProcess(exit_status=None, exit_signal=("TERM", False, "", "")))
    res_sig = await execute_vps_command(
        operation_id="op-t22-signal",
        host="127.0.0.1",
        port=22,
        username="root",
        password="pw",
        command="sleep 10",
        timeout_seconds=10.0,
        cleanup_timeout=0.1,
        dispatch_fence=ExecDispatchFence(),
        connector=lambda **_kw: asyncio.sleep(
            0, result=VPSSSHConnection(sig_conn, authentication_method="password")
        ),
    )
    assert res_sig["remote_execution_state"] == "completed"
    assert res_sig["exit_signal"] == "TERM"
    assert res_sig["exit_code"] is None

    # 4) Missing exit status & missing exit signal -> remote_outcome_unknown, exit_code=None.
    unknown_conn = HungCloseConn(FakeProcess(exit_status=None, exit_signal=None))
    res_unk = await execute_vps_command(
        operation_id="op-t22-missing-exit",
        host="127.0.0.1",
        port=22,
        username="root",
        password="pw",
        command="mystery",
        timeout_seconds=10.0,
        cleanup_timeout=0.1,
        dispatch_fence=ExecDispatchFence(),
        connector=lambda **_kw: asyncio.sleep(
            0, result=VPSSSHConnection(unknown_conn, authentication_method="password")
        ),
    )
    assert res_unk["remote_execution_state"] == "remote_outcome_unknown"
    assert res_unk["exit_code"] is None

    # 5) Cancellation while close hangs -> bounded cleanup finishes promptly.
    cancel_flag = [False]
    slow_proc = FakeProcess(exit_status=None, wait_delay=10.0)
    cancel_conn = HungCloseConn(slow_proc)

    async def trigger_cancel():
        await asyncio.sleep(0.1)
        cancel_flag[0] = True

    asyncio.create_task(trigger_cancel())
    res_cancel = await execute_vps_command(
        operation_id="op-t22-cancel-hung-close",
        host="127.0.0.1",
        port=22,
        username="root",
        password="pw",
        command="long-task",
        timeout_seconds=10.0,
        cleanup_timeout=0.15,
        cancelled=lambda: cancel_flag[0],
        dispatch_fence=ExecDispatchFence(),
        connector=lambda **_kw: asyncio.sleep(
            0, result=VPSSSHConnection(cancel_conn, authentication_method="password")
        ),
    )
    assert res_cancel["remote_execution_state"] == "cancelled"
    assert res_cancel["cancelled"] is True
    assert res_cancel["exit_code"] is None
    assert slow_proc.terminated is True
    assert cancel_conn.aborted is True


@pytest.mark.asyncio
async def test_t23_cross_chunk_password_redaction_and_bounded_tail_output():
    """T23: Password split across read chunks is redacted and large output preserves bounded tail."""
    ssh = pytest.importorskip("asyncssh")
    secret = "SuperSecretPass_987654321!"

    async def handle(chan, _username: str, _command: str):
        # Emit large filler followed by password split across two chunks and a final tail marker.
        chan.write(b"A" * 4096)
        await asyncio.sleep(0.01)
        chan.write(f"prefix-{secret[:12]}".encode())
        await asyncio.sleep(0.01)
        chan.write(f"{secret[12:]}-TAIL_MARKER\n".encode())
        chan.exit(0)

    Peer = _make_exec_server(ssh, users={"root": secret}, handler=handle)
    key = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(
        Peer, "127.0.0.1", 0, server_host_keys=[key], encoding=None
    )
    try:
        result = await execute_vps_command(
            operation_id="op-t23-redact-tail",
            host="127.0.0.1",
            port=listener.get_port(),
            username="root",
            password=secret,
            command="emit-secret-and-tail",
            timeout_seconds=15.0,
            output_limit=1024,
            dispatch_fence=ExecDispatchFence(),
        )
        assert result["exit_code"] == 0
        assert result["output_truncated"] is True
        assert len(result["output"]) <= 1024
        assert "TAIL_MARKER" in result["output"]
        assert "<redacted>" in result["output"]
        assert secret not in result["output"]
        assert secret[:12] not in result["output"]
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_interleaved_streams_decode_and_redact_independently():
    secret = "fixture-secret-only"
    stdout_text, stderr_text = "中文", "提示"
    stdout, stderr = stdout_text.encode(), stderr_text.encode()
    raw_chunks = [
        stdout[:1],
        stderr[:2],
        stdout[1:] + secret[:8].encode(),
        stderr[2:] + b"\n",
        secret[8:].encode() + b" tail",
    ]
    ready = [asyncio.Event() for _ in range(len(raw_chunks) + 1)]
    ready[0].set()
    streamed, snapshots = [], []

    class Stream:
        def __init__(self, *indexes):
            self.indexes = iter(indexes)

        async def read(self, _size):
            index = next(self.indexes, None)
            if index is None:
                return b""
            await ready[index].wait()
            ready[index + 1].set()
            return raw_chunks[index]

    class Process:
        stdout = Stream(0, 2, 4)
        stderr = Stream(1, 3)
        exit_status = 0

        async def wait(self):
            await ready[-1].wait()

    class Connection:
        async def create_process(self, *_args, **_kwargs):
            return Process()

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(**_kwargs):
        return VPSSSHConnection(Connection(), authentication_method="password")

    def collect_snapshot(output, final):
        snapshots.append((output, final))

    result = await execute_vps_command(
        operation_id="op-interleaved-output",
        host="127.0.0.1",
        port=22,
        username="fixture",
        password=secret,
        command="fixture only",
        timeout_seconds=5,
        connector=connect,
        dispatch_fence=ExecDispatchFence(),
        on_chunk_text=streamed.append,
        on_output=collect_snapshot,
    )

    expected = f"{stderr_text}\n{stdout_text}<redacted> tail"
    assert result["exit_code"] == 0
    assert result["output"] == expected
    assert "".join(streamed) == expected
    assert snapshots[-1] == (expected, True)
    assert all(secret not in output for output, _final in snapshots)
    raw = b"".join(raw_chunks)
    assert result["output_total_bytes"] == len(raw)
    assert result["output_total_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.asyncio
async def test_pre_command_transient_connect_retry_succeeds_without_retrying_auth():
    """Transient connect failure retries within budget; auth failure stops immediately."""
    ssh = pytest.importorskip("asyncssh")
    pw = token_urlsafe(10)
    exec_count = 0

    async def handle(chan, _username: str, _command: str):
        nonlocal exec_count
        exec_count += 1
        chan.write(b"ok-after-retry\n")
        chan.exit(0)

    Peer = _make_exec_server(ssh, users={"root": pw}, handler=handle)
    key = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(
        Peer, "127.0.0.1", 0, server_host_keys=[key], encoding=None
    )
    port = listener.get_port()

    connect_attempts = 0

    async def flaky_connect(**kwargs: Any):
        nonlocal connect_attempts
        connect_attempts += 1
        if connect_attempts == 1:
            raise VPSSSHConnectionError(
                "SSH_CONNECTION_FAILED", "transient reset", failure_stage="connection", retryable=True
            )
        from agent.vps_ssh import connect_agent_vps

        return await connect_agent_vps(**kwargs)

    try:
        res = await execute_vps_command(
            operation_id="op-retry-transient",
            host="127.0.0.1",
            port=port,
            username="root",
            password=pw,
            command="echo ok",
            timeout_seconds=15.0,
            retry_delays=(0.02, 0.02),
            dispatch_fence=ExecDispatchFence(),
            connector=flaky_connect,
        )
        assert connect_attempts == 2
        assert exec_count == 1
        assert res["exit_code"] == 0
        assert "ok-after-retry" in res["output"]
    finally:
        listener.close()
        await listener.wait_closed()
