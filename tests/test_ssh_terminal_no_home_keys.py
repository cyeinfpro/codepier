"""The legacy Agent transport does not consult local key discovery."""

import pytest

from agent.ssh_terminal_transport import AsyncSSHTransport
from shared.ssh_terminal_config import TerminalConfig


@pytest.mark.asyncio
async def test_agent_password_auth_keeps_local_key_discovery_disabled(monkeypatch, tmp_path):
    ssh = pytest.importorskip("asyncssh")
    import asyncssh.connection as ssh_connection

    def forbidden(*args, **kwargs):
        pytest.fail("AsyncSSH unexpectedly invoked a home-key discovery loader")

    for loader in (
        "load_default_keypairs",
        "load_default_identities",
        "load_default_host_public_keys",
    ):
        monkeypatch.setattr(ssh_connection, loader, forbidden)

    key = ssh.generate_private_key("ssh-ed25519")
    password_events = []

    class Peer(ssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            password_events.append((username, password == "isolated-fixture"))
            return username == "fixture" and password == "isolated-fixture"

    peer = await ssh.create_server(Peer, "127.0.0.1", 0, server_host_keys=[key])
    trust = tmp_path / "known_hosts"
    trust.write_bytes(f"[127.0.0.1]:{peer.get_port()} ".encode() + key.export_public_key("openssh"))
    transport = AsyncSSHTransport(TerminalConfig(), trust_sources=[trust])
    try:
        await transport.connect(
            {
                "host": "127.0.0.1",
                "port": peer.get_port(),
                "username": "fixture",
                "host_key_policy": "strict",
            },
            "isolated-fixture",
        )
        assert password_events == [("fixture", True)]
    finally:
        await transport.close()
        peer.close()
        await peer.wait_closed()
