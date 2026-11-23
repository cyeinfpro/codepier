"""Supplied-socket SSH readiness invariants; no production access."""

import asyncio
import base64
import socket
from types import SimpleNamespace

import pytest

from hub import vps_readiness_ssh as native


def fingerprint(value):
    return "SHA256:" + base64.b64encode(bytes([value]) * 32).decode().rstrip("=")


@pytest.mark.asyncio
async def test_connector_pins_endpoint_before_single_credential_read_and_disables_local_discovery(
    monkeypatch,
):
    messages = []
    target = SimpleNamespace(
        host="synthetic.invalid", port=2222, username="fixture", host_fingerprint=fingerprint(1)
    )

    class Socket:
        family = socket.AF_INET
        closed = False

        def getpeername(self):
            return "192.0.2.10", target.port

        def close(self):
            self.closed = True

    class Connection:
        def close(self):
            messages.append("closed")

        async def wait_closed(self):
            return None

    sock = Socket()

    async def password():
        messages.append("credential")
        return "test-fixture"

    async def fake_connect(**kwargs):
        assert kwargs["sock"] is sock
        assert (kwargs["host"], kwargs["port"], kwargs["username"]) == (
            target.host,
            target.port,
            target.username,
        )
        assert kwargs["config"] is None and kwargs["known_hosts"] == b""
        assert all(
            kwargs[name] is None
            for name in (
                "client_keys",
                "client_host_keys",
                "agent_path",
                "agent_identities",
                "pkcs11_provider",
            )
        )
        assert kwargs["client_host_keysign"] is False
        assert kwargs["password"] is None and kwargs["preferred_auth"] == ["password"]
        assert not any(
            kwargs[name]
            for name in (
                "public_key_auth",
                "host_based_auth",
                "kbdint_auth",
                "gss_auth",
                "gss_kex",
                "agent_forwarding",
                "canonicalize_hostname",
                "tunnel",
                "proxy_command",
            )
        )
        client = kwargs["client_factory"]()
        key = SimpleNamespace(get_fingerprint=lambda _: target.host_fingerprint)
        assert client.validate_host_public_key(target.host, "192.0.2.10", target.port, key)
        assert messages == []
        assert await client.password_auth_requested() == "test-fixture"
        assert await client.password_auth_requested() is None
        return Connection()

    monkeypatch.setattr(
        native.importlib,
        "import_module",
        lambda _: SimpleNamespace(
            SSHClient=type("SSHClient", (), {}),
            connect=fake_connect,
        ),
    )
    established = await native.connect_readiness(
        target,
        sock,
        password=password,
        verify_host=lambda pin: messages.append(("verified", pin)),
    )
    assert messages == [("verified", target.host_fingerprint), "credential"]
    await established.close()
    assert messages[-1] == "closed"


@pytest.mark.asyncio
async def test_real_loopback_uses_supplied_socket_with_invalid_dns_name_and_verified_pin(
    monkeypatch,
):
    ssh = pytest.importorskip("asyncssh")
    import asyncssh.connection as ssh_connection

    def forbidden(*args, **kwargs):
        pytest.fail("AsyncSSH must never discover local keys or agent identities")

    monkeypatch.setattr(ssh_connection, "load_default_keypairs", forbidden)
    monkeypatch.setattr(ssh_connection, "load_default_identities", forbidden)
    monkeypatch.setattr(ssh_connection, "load_default_host_public_keys", forbidden)
    host_key = ssh.generate_private_key("ssh-ed25519")
    supplied = []
    seen = []

    class Peer(ssh.SSHServer):
        def begin_auth(self, username):
            seen.append(username)
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "fixture" and password == "only-test"

    async def run(process):
        assert process.term_type is None
        assert process.command == "fixture-readiness"
        process.stdout.write("ok")
        process.exit(0)

    listener = await ssh.create_server(
        Peer,
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        process_factory=run,
    )
    target = SimpleNamespace(
        host="intentionally-nonexistent-readiness.invalid",
        port=listener.get_port(),
        username="fixture",
        host_fingerprint=host_key.get_fingerprint("sha256"),
    )

    async def dial():
        candidate = socket.socket()
        candidate.setblocking(False)
        await asyncio.get_running_loop().sock_connect(candidate, ("127.0.0.1", target.port))
        return candidate

    async def password():
        supplied.append("secret")
        return "only-test"

    try:
        async with await native.connect_readiness(
            target,
            await dial(),
            verify_host=lambda pin: supplied.append(pin),
            password=password,
        ) as con:
            assert await con.run_control("fixture-readiness") == (0, b"ok")
        assert supplied == [target.host_fingerprint, "secret"]
        assert seen == ["fixture"]
        stored = target.host_fingerprint
        target.host_fingerprint = fingerprint(2)
        with pytest.raises(native.SSHTransportError) as denied:
            await native.connect_readiness(
                target, await dial(), verify_host=pytest.fail, password=password
            )
        assert denied.value.code == "HOST_KEY_CHANGED"
        assert supplied == [stored, "secret"]
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_real_none_auth_still_cryptographically_checks_pin_without_reading_password():
    ssh = pytest.importorskip("asyncssh")
    host_key = ssh.generate_private_key("ssh-ed25519")
    verified = []

    class Peer(ssh.SSHServer):
        def begin_auth(self, username):
            assert username == "fixture"
            return False

    listener = await ssh.create_server(Peer, "127.0.0.1", 0, server_host_keys=[host_key])
    try:
        target = SimpleNamespace(
            host="also-intentionally-nonexistent.invalid",
            port=listener.get_port(),
            username="fixture",
            host_fingerprint=host_key.get_fingerprint("sha256"),
        )
        supplied_socket = socket.socket()
        supplied_socket.setblocking(False)
        await asyncio.get_running_loop().sock_connect(supplied_socket, ("127.0.0.1", target.port))

        async def password():
            pytest.fail("Password callback must not run for SSH none auth")

        async with await native.connect_readiness(
            target,
            supplied_socket,
            verify_host=verified.append,
            password=password,
        ):
            assert verified == [target.host_fingerprint]
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["manual_fleet", "managed_vps"])
async def test_real_managed_vps_uses_vetted_socket_without_adopting_the_key(purpose, monkeypatch):
    ssh = pytest.importorskip("asyncssh")
    host_key = ssh.generate_private_key("ssh-ed25519")
    seen = []

    class Peer(ssh.SSHServer):
        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            seen.append((username, password))
            return username == "fixture" and password == "only-test"

    listener = await ssh.create_server(Peer, "127.0.0.1", 0, server_host_keys=[host_key])
    target = SimpleNamespace(
        host="only-in-vetted-socket.invalid",
        port=listener.get_port(),
        username="fixture",
        host_fingerprint="",
        host_verification="required",
    )
    observations = []

    async def dial():
        sock = socket.socket()
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", target.port))
        return sock

    async def password():
        observations.append("credential")
        return "only-test"

    args = {"verify_host": observations.append, "password": password}
    try:
        with pytest.raises(native.SSHTransportError) as missing:
            await native.connect_readiness(target, await dial(), **args)
        assert missing.value.code == "HOST_KEY_UNKNOWN"
        assert observations == seen == []

        target.host_verification = "invalid_mode"
        with pytest.raises(native.SSHTransportError):
            await native.connect_readiness(target, await dial(), **args)
        assert observations == seen == []

        target.host_verification = "required"
        target.host_fingerprint = fingerprint(2)
        with pytest.raises(native.SSHTransportError) as mismatch:
            await native.connect_readiness(target, await dial(), **args)
        assert mismatch.value.code == "HOST_KEY_CHANGED"
        assert observations == seen == []

        target.host_verification = "skip"
        with pytest.raises(native.SSHTransportError):
            await native.connect_readiness(target, await dial(), **args)
        assert observations == seen == []

        target.purpose = purpose

        def forbid_fingerprint(*_, **__):
            pytest.fail("skip mode derived a host fingerprint")

        monkeypatch.setattr(ssh.SSHKey, "get_fingerprint", forbid_fingerprint)
        async with await native.connect_readiness(target, await dial(), **args) as transport:
            assert transport.verified_tmux_endpoint is None
        assert observations == [None, "credential"]
        assert seen == [("fixture", "only-test")]
    finally:
        listener.close()
        await listener.wait_closed()
