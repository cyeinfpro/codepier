"""Native readiness security/lifecycle tests. No actual sockets or credentials."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.ssh_terminal_transport import SSHTransportError
from hub.vps_readiness_native import (
    NativeSSHTarget,
    NativeVPSReadiness,
    PrivateSSHEndpoint,
    ReadinessNativeError,
)


_PIN = "SHA256:" + "A" * 43
_OTHER_PIN = "SHA256:" + "B" * 42 + "A"


class FakeSocket:
    def __init__(self, peer):
        self.peer = peer
        self.closed = False

    def getpeername(self):
        return self.peer

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.probes = 0
        self.closed = False

    async def run_control(self, command, *, max_bytes):
        assert "command -v tmux" in command and max_bytes == 1024
        self.probes += 1
        return self.response

    async def close(self):
        self.closed = True


class World:
    def __init__(self):
        self.job = SimpleNamespace(
            id="one",
            space_id="s",
            vps_id="v",
            owner_user_id="owner",
            connection_revision=3,
            grant_id="grant",
            authorized_user_id="actor",
        )
        self.target = NativeSSHTarget(
            job_id="one",
            space_id="s",
            vps_id="v",
            owner_user_id="owner",
            connection_revision=3,
            grant_id="grant",
            authorized_user_id="actor",
            host="node.example.com",
            port=2222,
            username="test-user",
            host_fingerprint=_PIN,
        )
        self.ip = "1.1.1.1"
        self.ip_override = None
        self.answers = None
        self.pin = _PIN
        self.calls = []
        self.transport = FakeTransport((0, b"/usr/bin/tmux\ntmux 3.4\n"))
        self.lookup_hook = self.dial_hook = self.secret_hook = self.install_hook = None
        self.socket = None
        self.use_password = True
        self.verify = True
        self.verify_after_secret = False

    async def lookup(self, job, action):
        assert job is self.job
        self.calls.append(action)
        return self.target

    async def secret(self, target):
        assert target is self.target
        self.calls.append("secret")
        if self.secret_hook:
            self.secret_hook()
        return "fictional-test-password"

    async def resolve(self, host, port):
        assert (host, port) == ("node.example.com", 2222)
        self.calls.append("resolve")
        if self.lookup_hook:
            self.lookup_hook()
        return self.answers if self.answers is not None else [self.ip]

    async def dial(self, ip, port):
        assert ip == self.ip and port == 2222
        self.calls.append("dial")
        self.socket = FakeSocket((self.ip_override or ip, port))
        if self.dial_hook:
            self.dial_hook()
        return self.socket

    async def connector(self, target, sock, *, verify_host, password):
        assert target == self.target and sock is self.socket
        self.calls.append("connector")
        if self.verify_after_secret:
            await password()
        if self.verify:
            verify_host(self.pin)
        if self.use_password:
            assert await password() == "fictional-test-password"
        return self.transport

    async def provision(self, transport, endpoint_key, *, authorize):
        assert transport is self.transport
        assert endpoint_key == ("node.example.com", 2222, "test-user")
        if self.install_hook:
            self.install_hook()
        assert await authorize() is True
        self.calls.append("installed")
        return {"available": True}

    def adapter(self, **values):
        options = dict(
            resolve_target=self.lookup,
            secret=self.secret,
            connector=self.connector,
            resolver=self.resolve,
            dial=self.dial,
            provisioner=self.provision,
        )
        return NativeVPSReadiness(**{**options, **values})


@pytest.mark.parametrize(
    "code,expected",
    [
        ("TMUX_INSTALL_PERMISSION", "install_not_started_denied"),
        ("TMUX_INSTALL_UNAVAILABLE", "install_not_started_unsupported"),
        ("TMUX_INSTALL_FAILED", "failed"),
        ("TMUX_INSTALL_UNCERTAIN", "uncertain"),
    ],
)
def test_provision_marks_only_precise_pre_install_transport_results(code, expected):
    world = World()

    async def provision(_transport, _endpoint_key, *, authorize):
        assert await authorize() is True
        raise SSHTransportError(code, "test")

    adapter = world.adapter(provisioner=provision)
    assert asyncio.run(adapter.provision(world.job)) == expected


def test_valid_saved_target_uses_vetted_socket_and_fixed_probe_without_agent():
    world = World()
    adapter = world.adapter()
    assert asyncio.run(adapter.authorize(world.job, "inspect")) is True
    assert asyncio.run(adapter.inspect(world.job)) == "ready"
    assert world.calls.count("resolve") == world.calls.count("dial") == 1
    assert world.calls.index("dial") < world.calls.index("connector") < world.calls.index("secret")
    assert world.transport.probes == 1
    assert world.socket.closed and world.transport.closed


def test_absent_dependencies_and_unmigrated_host_trust_fail_without_connection():
    world = World()
    assert asyncio.run(world.adapter(connector=None).inspect(world.job)) == "unsupported"
    assert asyncio.run(world.adapter(provisioner=None).provision(world.job)) == "unsupported"
    world.target = replace(world.target, host_fingerprint="")
    adapter = world.adapter()
    with pytest.raises(ReadinessNativeError) as raised:
        asyncio.run(adapter.require_target(world.job, "inspect"))
    assert raised.value.code == "HUB_SSH_TRUST_MIGRATION_REQUIRED"
    assert asyncio.run(adapter.authorize(world.job, "inspect")) is False
    assert asyncio.run(adapter.inspect(world.job)) == "denied"
    assert set(world.calls) == {"inspect", "install"}


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1",
        "169.254.169.254",
        "168.63.129.16",
        "10.0.0.2",
        "::ffff:127.0.0.1",
        "64:ff9b::a00:1",
        "2002:c0a8::1",
        "ff02::1",
        "fe80::1%eth0",
        "node.internal",
    ],
)
def test_all_dns_answers_are_validated_before_the_first_numeric_socket(value):
    world = World()
    world.answers = [world.ip, value]
    assert asyncio.run(world.adapter().inspect(world.job)) == "denied"
    assert "dial" not in world.calls and "secret" not in world.calls


def test_private_rule_requires_exact_space_host_port_and_ip():
    world = World()
    world.ip = "10.1.2.3"
    wrong = PrivateSSHEndpoint("s", "node.example.com", 22, world.ip)
    assert asyncio.run(world.adapter(private_allowlist=(wrong,)).inspect(world.job)) == "denied"
    allow = PrivateSSHEndpoint("s", "node.example.com", 2222, world.ip)
    assert asyncio.run(world.adapter(private_allowlist=(allow,)).inspect(world.job)) == "ready"
    with pytest.raises(ValueError):
        PrivateSSHEndpoint("s", "node.example.com", 2222, "169.254.169.254")


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "different"),
        ("authorized_user_id", "other"),
        ("connection_revision", 4),
        ("space_id", "other"),
        ("grant_id", ""),
    ],
)
def test_current_scope_must_match_the_immutable_queue_job(field, value):
    world = World()
    world.target = replace(world.target, **{field: value})
    assert asyncio.run(world.adapter().authorize(world.job, "install")) is False
    assert asyncio.run(world.adapter().provision(world.job)) == "denied"
    assert set(world.calls) == {"install"}


def test_mismatched_peer_and_unverified_or_wrong_host_never_release_secret():
    for variation in ("peer", "pin", "before-proof", "auth-none"):
        world = World()
        if variation == "peer":
            world.ip_override = "8.8.8.8"
        elif variation == "pin":
            world.pin = _OTHER_PIN
        elif variation == "before-proof":
            world.verify_after_secret = True
        else:
            world.verify = world.use_password = False
        assert asyncio.run(world.adapter().inspect(world.job)) == "denied"
        assert "secret" not in world.calls and world.transport.probes == 0
        assert world.socket.closed
    world = World()
    world.use_password = False
    assert asyncio.run(world.adapter().inspect(world.job)) == "ready"
    assert "secret" not in world.calls


def test_target_or_grant_change_fences_dns_dial_and_authenticated_control():
    for hook_name, forbidden in (
        ("lookup_hook", "dial"),
        ("dial_hook", "connector"),
        ("secret_hook", "installed"),
    ):
        world = World()
        setattr(
            world,
            hook_name,
            lambda: setattr(world, "target", replace(world.target, host="other.example.com")),
        )
        assert asyncio.run(world.adapter().provision(world.job)) == "denied"
        assert forbidden not in world.calls
        assert world.socket is None or world.socket.closed


def test_live_install_grant_is_rechecked_after_open_before_package_action():
    world = World()
    world.install_hook = lambda: setattr(world, "target", replace(world.target, grant_id="revoked"))
    assert asyncio.run(world.adapter().provision(world.job)) == "denied"
    assert "secret" in world.calls and "installed" not in world.calls
    assert world.transport.closed and world.socket.closed

    world = World()
    assert asyncio.run(world.adapter().provision(world.job)) == "ok"
    assert world.calls[-2:] == ["install", "installed"]


@pytest.mark.parametrize("answer", [False, None, RuntimeError("revoked"), asyncio.CancelledError()])
def test_real_tmux_helper_fences_async_authorization_before_package_dispatch(answer):
    from agent.ssh_terminal_tmux import provision_tmux
    from agent.ssh_terminal_transport import SSHTransportError

    class Missing:
        calls = []

        async def run_control(self, command, **kwargs):
            self.calls.append(command)
            assert "command -v tmux" in command
            return 127, b""

    async def authorize():
        await asyncio.sleep(0)  # async persistence boundary
        if isinstance(answer, BaseException):
            raise answer
        return answer

    transport = Missing()
    exception = type(answer) if isinstance(answer, BaseException) else SSHTransportError
    with pytest.raises(exception):
        asyncio.run(
            provision_tmux(
                transport,
                ("simulated.example", 22, "no-network"),
                authorize=authorize,
            )
        )
    assert len(transport.calls) == 1
