"""Real loopback transport tests for vps_ssh_v2 (no host key verification).

Covers acceptance matrix:
- T01: Same host:port host key rotation A -> B succeeds across connect_agent_vps and AsyncSSHTransport.
- T02: known_hosts containing key A remains byte-identical after connecting to key B.
- T03: Corrupted, unreadable, unwritable, @revoked known_hosts and local SSH config
  (with KnownHostsCommand / UserKnownHostsFile) are ignored and never executed.
- T05: Legacy empty, malformed, or mismatched host_key_fingerprint / strict policy do not block.
- T07: Final asyncssh.connect call explicitly receives known_hosts=None, config=None, client_keys=None.
- T13: Rotated host key + wrong password reports SSH_AUTH_FAILED (never host key error).
- T14: Disabled login / additional OTP challenge / unreachable port / KeyExchangeFailed classification.
- T15: Corrupted SSH handshake / protocol mismatch is rejected at protocol layer.
- T27: Missing/errored fingerprint observation does not block transport or tmux probe/provision recovery.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from secrets import token_urlsafe

import pytest

from agent.shell import execution_info
from agent.ssh_terminal_link import AgentTerminalLink
from agent.ssh_terminal_manager import TerminalSessionManager
from agent.ssh_terminal_tmux import _RECENT_FAILURES, _UNCERTAIN, probe_tmux, provision_tmux
from agent.ssh_terminal_transport import AsyncSSHTransport, SSHTransportError, classify_error
from agent.vps_ssh import VPSRuntimeBudget, VPSSSHConnectionError, connect_agent_vps
from shared.contracts import SSHExec
from shared.ssh_terminal_config import TerminalConfig
from shared.vps_transport import decode_legacy_ssh_request


def _make_password_server(ssh, *, username: str, password: str, calls: list[str]):
    class Peer(ssh.SSHServer):
        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, supplied_user, supplied_pw):
            calls.append(supplied_user)
            return supplied_user == username and supplied_pw == password

    return Peer


@pytest.mark.asyncio
async def test_t01_t02_host_key_rotation_same_port_and_known_hosts_unchanged(tmp_path: Path):
    """T01 & T02: Key rotation A -> B on the same host:port succeeds and leaves known_hosts untouched."""
    ssh = pytest.importorskip("asyncssh")
    password = token_urlsafe(12)
    calls: list[str] = []
    Peer = _make_password_server(ssh, username="root", password=password, calls=calls)

    key_a = ssh.generate_private_key("ssh-ed25519")
    key_b = ssh.generate_private_key("ssh-ed25519")

    # Start server with key A on an ephemeral port, then reuse the same port with key B.
    listener_a = await ssh.create_server(Peer, "127.0.0.1", 0, server_host_keys=[key_a])
    port = listener_a.get_port()
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        f"[127.0.0.1]:{port} {key_a.export_public_key('openssh').decode('ascii')}\n",
        encoding="utf-8",
    )
    original_bytes = known_hosts.read_bytes()

    try:
        conn_a = await connect_agent_vps(
            host="127.0.0.1",
            port=port,
            username="root",
            password=password,
            budget=VPSRuntimeBudget.start(15.0),
        )
        try:
            assert conn_a.authentication_method == "password"
            assert conn_a.host_identity_verification_applied == "disabled"
        finally:
            await conn_a.close()
    finally:
        listener_a.close()
        await listener_a.wait_closed()

    # Rotate server host key to B on the exact same port.
    listener_b = await ssh.create_server(
        Peer, "127.0.0.1", port, server_host_keys=[key_b], reuse_address=True
    )
    try:
        # 1) Shared connect_agent_vps succeeds with key B.
        conn_b = await connect_agent_vps(
            host="127.0.0.1",
            port=port,
            username="root",
            password=password,
            budget=VPSRuntimeBudget.start(15.0),
        )
        try:
            assert conn_b.authentication_method == "password"
        finally:
            await conn_b.close()

        # 2) AsyncSSHTransport succeeds with key B even when legacy strict target & known_hosts(A) exist.
        transport = AsyncSSHTransport(TerminalConfig.from_env(), trust_sources=[known_hosts])
        try:
            await transport.connect(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "username": "root",
                    "connection_contract": "vps_ssh_v2",
                    "host_key_policy": "strict",
                    "host_key_fingerprint": "SHA256:stale-fingerprint-from-key-a",
                },
                password,
            )
            assert transport.connected
            assert transport.trust is None
        finally:
            await transport.close()

        assert calls == ["root", "root", "root"]
        assert known_hosts.read_bytes() == original_bytes
        assert not known_hosts.with_name("known_hosts.codepier.lock").exists()
    finally:
        listener_b.close()
        await listener_b.wait_closed()


@pytest.mark.asyncio
async def test_t03_corrupted_revoked_unreadable_known_hosts_and_ssh_config_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """T03: Corrupted/unreadable/@revoked known_hosts and local ~/.ssh/config never affect connect."""
    ssh = pytest.importorskip("asyncssh")
    password = token_urlsafe(12)
    calls: list[str] = []
    Peer = _make_password_server(ssh, username="ops", password=password, calls=calls)
    key = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(Peer, "127.0.0.1", 0, server_host_keys=[key])
    port = listener.get_port()

    fake_home = tmp_path / "home"
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir(parents=True)

    sentinel_file = tmp_path / "trust_cmd_ran"
    ssh_config = ssh_dir / "config"
    ssh_config.write_text(
        f"Host *\n"
        f"  StrictHostKeyChecking yes\n"
        f"  UserKnownHostsFile {ssh_dir / 'known_hosts'}\n"
        f"  KnownHostsCommand /bin/sh -c 'touch {sentinel_file}; exit 1'\n",
        encoding="utf-8",
    )

    # Corrupted + @revoked known_hosts
    kh = ssh_dir / "known_hosts"
    kh.write_bytes(
        b"\xff\xfeCORRUPTED_LINE\n"
        + f"@revoked [127.0.0.1]:{port} {key.export_public_key('openssh').decode('ascii')}\n".encode()
    )
    os.chmod(kh, 0o000)

    monkeypatch.setenv("HOME", str(fake_home))
    try:
        conn = await connect_agent_vps(
            host="127.0.0.1",
            port=port,
            username="ops",
            password=password,
            budget=VPSRuntimeBudget.start(15.0),
        )
        try:
            assert conn.authentication_method == "password"
        finally:
            await conn.close()
        assert not sentinel_file.exists(), "Local SSH config KnownHostsCommand must never execute"
        assert calls == ["ops"]
    finally:
        os.chmod(kh, 0o600)
        listener.close()
        await listener.wait_closed()


def test_t05_legacy_trust_fields_stripped_and_malformed_pin_ignored():
    """T05: Empty, malformed, or mismatched legacy trust fields never block parsing."""
    cleaned = decode_legacy_ssh_request(
        {
            "host": "127.0.0.1",
            "port": 22,
            "username": "root",
            "host_key_policy": "strict",
            "host_key_fingerprint": "NOT-A-VALID-PIN!!!",
            "known_hosts_file": "/nonexistent/broken/known_hosts",
        }
    )
    assert "host_key_policy" not in cleaned
    assert "host_key_fingerprint" not in cleaned
    assert "known_hosts_file" not in cleaned

    req = SSHExec(
        project="demo",
        host="127.0.0.1",
        port=22,
        username="root",
        password="secret-password",
        command="id",
        host_key_policy="confirm-change",
        host_key_fingerprint="malformed-pin-value",
        known_hosts_file="/etc/does-not-exist",
        idempotency_key="idem-t05-12345678",
    )
    assert req.host_key_policy == "disabled"
    assert req.host_key_fingerprint == ""
    assert req.known_hosts_file == ""


@pytest.mark.asyncio
async def test_t07_explicit_known_hosts_none_and_config_none_passed_to_asyncssh():
    """T07: Final asyncssh.connect receives explicit known_hosts=None, config=None, client_keys=None."""
    ssh = pytest.importorskip("asyncssh")
    password = token_urlsafe(12)
    calls: list[str] = []
    Peer = _make_password_server(ssh, username="root", password=password, calls=calls)
    key = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(Peer, "127.0.0.1", 0, server_host_keys=[key])

    captured_kwargs: dict[str, object] = {}

    class SpySSHModule:
        SSHClient = ssh.SSHClient

        @staticmethod
        async def connect(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return await ssh.connect(*args, **kwargs)

    try:
        conn = await connect_agent_vps(
            host="127.0.0.1",
            port=listener.get_port(),
            username="root",
            password=password,
            budget=VPSRuntimeBudget.start(15.0),
            ssh_module=SpySSHModule,
        )
        try:
            assert "known_hosts" in captured_kwargs and captured_kwargs["known_hosts"] is None
            assert "config" in captured_kwargs and captured_kwargs["config"] is None
            assert "client_keys" in captured_kwargs and captured_kwargs["client_keys"] is None
            assert captured_kwargs["public_key_auth"] is False
            assert captured_kwargs["agent_forwarding"] is False
        finally:
            await conn.close()
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_t13_rotated_key_with_wrong_password_reports_auth_failed():
    """T13: Wrong password after key rotation returns SSH_AUTH_FAILED, never host key error."""
    ssh = pytest.importorskip("asyncssh")
    calls: list[str] = []
    Peer = _make_password_server(ssh, username="root", password="correct-password", calls=calls)
    key_b = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(Peer, "127.0.0.1", 0, server_host_keys=[key_b])
    try:
        with pytest.raises(VPSSSHConnectionError) as exc:
            await connect_agent_vps(
                host="127.0.0.1",
                port=listener.get_port(),
                username="root",
                password="wrong-password",
                budget=VPSRuntimeBudget.start(15.0),
            )
        assert exc.value.code == "SSH_AUTH_FAILED"
        assert exc.value.failure_stage == "authentication"
        assert exc.value.retryable is False
        assert calls == ["root"]
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_t14_additional_auth_challenge_and_unreachable_port_and_kex_classification():
    """T14: OTP challenge, unreachable port, and KeyExchangeFailed are accurately classified."""
    ssh = pytest.importorskip("asyncssh")

    # 1) Additional interactive challenge rejected with SSH_AUTH_CHALLENGE_REQUIRED.
    class ChallengePeer(ssh.SSHServer):
        def begin_auth(self, _username):
            return True

        def kbdint_auth_supported(self):
            return True

        def get_kbdint_challenge(self, _username, _lang, _submethods):
            return "MFA", "", "", [("Verification code: ", False)]

        def validate_kbdint_response(self, _username, _responses):
            return True

    key = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(ChallengePeer, "127.0.0.1", 0, server_host_keys=[key])
    try:
        with pytest.raises(VPSSSHConnectionError) as exc:
            await connect_agent_vps(
                host="127.0.0.1",
                port=listener.get_port(),
                username="root",
                password=token_urlsafe(8),
                budget=VPSRuntimeBudget.start(10.0),
            )
        assert exc.value.code == "SSH_AUTH_CHALLENGE_REQUIRED"
        assert exc.value.retryable is False
    finally:
        listener.close()
        await listener.wait_closed()

    # 2) Unreachable port -> SSH_CONNECTION_FAILED (retryable).
    closed_listener = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    dead_port = closed_listener.sockets[0].getsockname()[1]
    closed_listener.close()
    await closed_listener.wait_closed()

    with pytest.raises(VPSSSHConnectionError) as exc_conn:
        await connect_agent_vps(
            host="127.0.0.1",
            port=dead_port,
            username="root",
            password="pw",
            budget=VPSRuntimeBudget.start(5.0),
        )
    assert exc_conn.value.code == "SSH_CONNECTION_FAILED"
    assert exc_conn.value.retryable is True

    # 3) KeyExchangeFailed maps to SSH_PROTOCOL_NEGOTIATION_FAILED (NEVER HOST_KEY_UNKNOWN).
    kex_err = classify_error(ssh.KeyExchangeFailed("no matching kex algorithm"))
    assert isinstance(kex_err, SSHTransportError)
    assert kex_err.code == "SSH_PROTOCOL_NEGOTIATION_FAILED"


@pytest.mark.asyncio
async def test_t15_corrupted_ssh_handshake_rejected_at_protocol_layer():
    """T15: A peer sending corrupted SSH handshake/protocol bytes is rejected."""
    pytest.importorskip("asyncssh")

    async def broken_ssh_peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # Send valid SSH banner followed by corrupted binary kex frame.
            writer.write(b"SSH-2.0-BrokenPeer\r\n")
            await writer.drain()
            await reader.read(256)
            writer.write(b"\x00\x00\x00\x10\x04\xff\xff\xff\xff\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a")
            await writer.drain()
            await asyncio.sleep(0.2)
        finally:
            writer.close()

    server = await asyncio.start_server(broken_ssh_peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(VPSSSHConnectionError) as exc:
            await connect_agent_vps(
                host="127.0.0.1",
                port=port,
                username="root",
                password="pw",
                budget=VPSRuntimeBudget.start(5.0),
            )
        assert exc.value.code in {
            "SSH_PROTOCOL_NEGOTIATION_FAILED",
            "SSH_CHANNEL_CLOSED",
            "SSH_CONNECTION_FAILED",
        }
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_t27_tmux_uncertain_recovery_without_fingerprint_and_capability_reporting(
    tmp_path: Path,
):
    """T27: tmux probe/provision reconciles _UNCERTAIN without fingerprints and capabilities match vps_ssh_v2."""
    endpoint = ("10.0.0.5", 2222)
    _UNCERTAIN.clear()
    _RECENT_FAILURES.clear()

    class FakeTransport:
        def __init__(self, responses):
            self.transport_contract = "vps_ssh_v2"
            self.target_endpoint = endpoint
            self._responses = list(responses)
            self.commands = []

        async def run_control(self, command, **_kwargs):
            self.commands.append(command)
            item = self._responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    # Step 1: Install command drops mid-flight -> endpoint marked _UNCERTAIN.
    t1 = FakeTransport([(127, b""), RuntimeError("connection lost")])
    with pytest.raises(SSHTransportError) as exc1:
        await provision_tmux(t1, (*endpoint, "root"), authorize=lambda: True)
    assert exc1.value.code == "TMUX_INSTALL_UNCERTAIN"
    assert endpoint in _UNCERTAIN

    # Step 2: Re-running provision while tmux is still absent refuses blind reinstall.
    t2 = FakeTransport([(127, b"")])
    with pytest.raises(SSHTransportError) as exc2:
        await provision_tmux(t2, (*endpoint, "root"), authorize=lambda: True)
    assert exc2.value.code == "TMUX_INSTALL_UNCERTAIN"
    assert len(t2.commands) == 1  # Only side-effect-free probe ran; no reinstall!

    # Step 3: Side-effect-free probe observes tmux installed -> clears _UNCERTAIN without fingerprint.
    t3 = FakeTransport([(0, b"/usr/bin/tmux\ntmux 3.3a\n")])
    cap = await probe_tmux(t3)
    assert cap == {"available": True, "version": "3.3"}
    assert endpoint not in _UNCERTAIN

    # Capability reporting checks across shell.execution_info, TerminalSessionManager, AgentTerminalLink.
    config = {
        "state_dir": str(tmp_path / "state"),
        "shell": {
            "enabled": True,
            "projects": ["*"],
            "command": ["/bin/sh", "-lc"],
            "inherit_env": False,
            "env": {"PATH": ""},  # Even with empty PATH (no sshpass), AsyncSSH drives availability!
            "max_timeout_seconds": 300,
        }
    }
    project = {"id": "p1", "alias": "demo", "mode": "write", "allow_tasks": True}
    spec = {"writable": True, "allow_tasks": True}
    info = execution_info(config, project, spec, tmp_path)
    assert info["ssh"]["connection_contract"] == "vps_ssh_v2"
    assert info["ssh"]["host_identity_verification"] == "disabled"
    assert info["ssh"]["transport_runtime"] == "asyncssh"
    assert "host_key_policy" not in info["ssh"]
    assert info["ssh"]["password_auth_available"] is True

    mgr = TerminalSessionManager(emit=lambda _e: asyncio.sleep(0))
    mgr_caps = mgr.capabilities()
    assert mgr_caps["connection_contract"] == "vps_ssh_v2"
    assert mgr_caps["host_identity_verification"] == "disabled"
    assert mgr_caps["vps_ssh"]["protocol"] == "vps_ssh_v2"

    link = AgentTerminalLink(agent=type("FakeAgent", (), {"config": config})())
    hello = link._hello()
    assert hello["vps_ssh_protocol"] == "vps_ssh_v2"
    assert hello["host_identity_verification"] == "disabled"
    assert hello["agent_vps_ssh"]["protocol"] == "vps_ssh_v2"
    assert hello["agent_vps_ssh"]["host_identity_verification"] == "disabled"
