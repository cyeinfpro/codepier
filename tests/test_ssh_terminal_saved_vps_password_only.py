"""Verify only the explicitly marked saved-VPS Agent transport skips host identity."""

from pathlib import Path
from secrets import token_urlsafe

import pytest

from agent.ssh_terminal_transport import AsyncSSHTransport, SSHTransportError
from shared.ssh_terminal_config import TerminalConfig


@pytest.mark.asyncio
async def test_real_saved_vps_uses_password_without_reading_or_writing_known_hosts(tmp_path: Path):
    ssh = pytest.importorskip("asyncssh")
    received = []
    password = token_urlsafe(9)

    class Peer(ssh.SSHServer):
        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, supplied):
            received.append(username)
            return username == "fixture" and supplied == password

    key = ssh.generate_private_key("ssh-ed25519")
    mismatch = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(Peer, "127.0.0.1", 0, server_host_keys=[key])
    stored_trust = tmp_path / "known_hosts"
    stored_trust.write_text(
        f"[127.0.0.1]:{listener.get_port()} {mismatch.export_public_key('openssh').decode('ascii')}"
    )
    original = stored_trust.read_bytes()
    target = {
        "host": "127.0.0.1",
        "port": listener.get_port(),
        "username": "fixture",
        "host_key_policy": "strict",
    }
    config = TerminalConfig.from_env()
    try:
        ordinary = AsyncSSHTransport(config, trust_sources=[stored_trust])
        with pytest.raises(SSHTransportError):
            await ordinary.connect(target, password)
        assert received == []
        assert stored_trust.read_bytes() == original

        saved = AsyncSSHTransport(config, trust_sources=[stored_trust])
        try:
            await saved.connect(dict(target, saved_vps_password_only=True), password)
            assert saved.connected and saved.trust is None
            assert received == ["fixture"]
        finally:
            await saved.close()
        assert stored_trust.read_bytes() == original
        assert not stored_trust.with_name("known_hosts.codepier.lock").exists()
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_saved_vps_refuses_additional_sensitive_prompts(tmp_path: Path):
    ssh = pytest.importorskip("asyncssh")
    received = []

    class Peer(ssh.SSHServer):
        def begin_auth(self, _username):
            return True

        def kbdint_auth_supported(self):
            return True

        def get_kbdint_challenge(self, _username, _lang, _submethods):
            return "Account recovery", "", "", [("One-time code: ", False)]

        def validate_kbdint_response(self, _username, responses):
            received.extend(responses)
            return True

    async def unexpected_challenge(*_):
        pytest.fail("unverified server prompted the user for additional credentials")

    key = ssh.generate_private_key("ssh-ed25519")
    listener = await ssh.create_server(Peer, "127.0.0.1", 0, server_host_keys=[key])
    target = {
        "host": "127.0.0.1",
        "port": listener.get_port(),
        "username": "fixture",
        "saved_vps_password_only": True,
    }
    transport = AsyncSSHTransport(TerminalConfig.from_env(), trust_sources=[tmp_path / "private"])
    try:
        with pytest.raises(SSHTransportError) as exc:
            await transport.connect(target, token_urlsafe(9), challenge=unexpected_challenge)
        assert exc.value.code == "SSH_AUTH_CHALLENGE_REQUIRED"
        assert received == []
    finally:
        listener.close()
        await listener.wait_closed()
