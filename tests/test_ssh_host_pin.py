"""Pinned SSH host keys verify before any credentials go to a synthetic peer."""

import pytest
from pydantic import ValidationError

from agent.ssh_terminal_trust import HostKeyPolicy, HostTrustError
from agent.ssh_terminal_transport import AsyncSSHTransport, SSHTransportError
from hub.vps import VPSInput
from shared.ssh_terminal_config import TerminalConfig


@pytest.mark.parametrize("pin", ["", "MD5:" + "01" * 16, "SHA256:" + "B" * 43])
def test_pin_mode_rejects_missing_or_malformed_fingerprint(pin):
    with pytest.raises(ValidationError):
        VPSInput(
            name="fixture",
            host="127.0.0.1",
            username="fixture",
            host_key_policy="confirm-change",
            host_key_fingerprint=pin,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exact", "mismatch", "revoked"])
async def test_configured_pin_before_auth_even_when_presented_key_is_known(tmp_path, mode):
    asyncssh = pytest.importorskip("asyncssh")
    current_key = asyncssh.generate_private_key("ssh-ed25519")
    old_key = asyncssh.generate_private_key("ssh-ed25519")
    auth_attempts = []

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, secret):
            auth_attempts.append((username, secret))
            return True

    peer = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[current_key])
    port = peer.get_port()
    ssh = tmp_path / "known_hosts"
    key_in_file = old_key if mode == "exact" else current_key
    marker = "@revoked " if mode == "revoked" else ""
    previous = f"{marker}[127.0.0.1]:{port} " + key_in_file.export_public_key().decode()
    ssh.write_text(previous)
    connector = AsyncSSHTransport(TerminalConfig(), trust_sources=(ssh,), trust_write_path=ssh)
    target = {
        "host": "127.0.0.1",
        "port": port,
        "username": "fixture",
        "host_key_policy": "confirm-change",
        "host_key_fingerprint": (old_key if mode == "mismatch" else current_key).get_fingerprint(),
    }
    try:
        if mode == "exact":
            await connector.connect(target, "fixture-secret")
            assert connector.connected and auth_attempts == [("fixture", "fixture-secret")]
        else:
            with pytest.raises(SSHTransportError) as error:
                await connector.connect(target, "fixture-secret")
            assert error.value.code == (
                "HOST_KEY_CHANGED" if mode == "mismatch" else "HOST_KEY_REVOKED"
            )
            assert not auth_attempts
        assert ssh.read_text() == previous
    finally:
        await connector.close()
        peer.close()
        await peer.wait_closed()


def test_pin_mode_trust_callback_filters_legacy_trust_and_honors_revocation():
    pin = "SHA256:" + "A" * 43

    class Key:
        def __init__(self, fingerprint):
            self.fingerprint = fingerprint

        def get_fingerprint(self):
            return self.fingerprint

    exact = Key(pin)
    known_but_wrong = Key("SHA256:" + "B" * 42 + "A")
    policy = HostKeyPolicy.__new__(HostKeyPolicy)
    policy.expected_fingerprint = pin
    policy.fingerprint = None
    policy.pending = None
    policy.error = None
    revoked = []
    policy.matched = lambda *args, **kwargs: ([known_but_wrong], ["CA"], revoked, [], [], [], [])
    assert policy.known_hosts("test.invalid", "", 22) == ([], [], [], [], [], [], [])
    assert not policy.validate_candidate("test.invalid", "", 22, known_but_wrong)
    assert policy.error.code == "HOST_KEY_CHANGED"
    with pytest.raises(HostTrustError):
        policy.commit_before_authentication()
    assert policy.validate_candidate("test.invalid", "", 22, exact)
    assert policy.pending is None
    policy.commit_before_authentication()
    revoked.append(exact)
    assert not policy.validate_candidate("test.invalid", "", 22, exact)
    assert policy.error.code == "HOST_KEY_REVOKED"


def test_pin_lives_on_the_vps_and_rotation_changes_the_connection_revision(tmp_path):
    from hub.runtime import Principal, Runtime
    from hub.store import Store
    from tests.legacy_iam_fixture import seed_owner

    store = Store(tmp_path / "data")
    seed_owner(store, "owner", "fixture")
    runtime = Runtime(store)
    owner = Principal("panel:fixture", "owner", {"read", "write", "execute"}, [], admin=True)
    try:
        common = dict(
            name="VPS", host="192.0.2.8", username="fixture", host_key_policy="confirm-change"
        )
        previous = "SHA256:" + "A" * 43
        updated = "SHA256:" + "B" * 42 + "A"
        saved = runtime.vps.save(
            VPSInput(**common, host_key_fingerprint=previous, password="synthetic"), owner
        )
        assert saved["host_key_fingerprint"] == previous
        row = store.one(
            "SELECT connection_revision FROM vps_connections WHERE id=?", (saved["id"],)
        )
        result = runtime.vps.save(
            VPSInput(**common, host_key_fingerprint=updated, expected_version=saved["version"]),
            owner,
            saved["id"],
        )
        assert result["host_key_fingerprint"] == updated
        latest = store.one(
            "SELECT connection_revision FROM vps_connections WHERE id=?", (saved["id"],)
        )
        assert latest["connection_revision"] == row["connection_revision"] + 1
    finally:
        store.close()


def test_host_pin_capability_is_opt_in_and_scoped_to_the_current_connection(monkeypatch):
    from types import SimpleNamespace

    from hub.ssh_terminal.service import TerminalService
    from hub.vps import _agent_probe_ready
    from shared.util import DevError

    old = SimpleNamespace(closed=False, probe_supported=True, direct_vps=True)
    new = SimpleNamespace(
        closed=False, probe_supported=True, direct_vps=True, host_pin_supported=True
    )
    malformed = SimpleNamespace(
        closed=False, probe_supported=True, direct_vps=True, host_pin_supported="true"
    )
    project = {"device_id": "old", "_direct_vps": True}
    assert _agent_probe_ready(old, project)
    assert not _agent_probe_ready(old, project, requires_pin=True)
    assert not _agent_probe_ready(malformed, project, requires_pin=True)
    assert _agent_probe_ready(new, project, requires_pin=True)

    terminal = TerminalService.__new__(TerminalService)
    monkeypatch.setenv("CODEPIER_SSH_TERMINAL_ENABLED", "1")
    terminal.closing = False
    terminal.config = TerminalConfig()
    terminal.agents = {"old": old, "new": new}
    devices = [{"id": "old"}, {"id": "new"}]
    assert terminal._select_admin_direct_device(devices, {"old"}, {})["id"] == "old"
    assert (
        terminal._select_admin_direct_device(devices, {"old"}, {}, requires_pin=True)["id"] == "new"
    )
    assert terminal._capability_reason(project, old, requires_pin=True) == "AGENT_UPGRADE_REQUIRED"
    assert terminal._capability_reason(project, old) is None

    pin_target = {"host_key_policy": "confirm-change"}
    with pytest.raises(DevError) as error:
        terminal._require_host_pin_agent(project, pin_target, old)
    assert error.value.code == "AGENT_UPGRADE_REQUIRED"
    assert terminal._require_host_pin_agent({"device_id": "new"}, pin_target, new) is None
    terminal.agents["new"] = old
    with pytest.raises(DevError):
        terminal._require_host_pin_agent({"device_id": "new"}, pin_target, new)
