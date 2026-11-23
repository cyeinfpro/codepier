"""Full manual fleet lifecycle over actual loopback SSH, with no real package writes."""

import pytest
from fastapi.testclient import TestClient

from agent.ssh_terminal_tmux import _INSTALL, _PROBE
from agent.ssh_terminal_tmux import provision_tmux
from hub.app import create_app
from hub.vps_readiness_native import NativeVPSReadiness
from hub.vps_readiness_ssh import connect_readiness
from tests import test_vps_readiness_wiring as wiring_fixture

ORIGIN = wiring_fixture.ORIGIN
HOST = "readiness-loopback.invalid"
LOGIN = "fake-installed-password"


class LoopbackScenario:
    def __init__(self, result):
        self.result = result
        self.installed = False
        self.listener = None
        self.auth_count = 0
        self.calls = []
        self.unexpected = []

    async def start(self, asyncssh, key):
        owner = self

        class Peer(asyncssh.SSHServer):
            def begin_auth(self, _username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                owner.auth_count += 1
                return username == "fixture" and password == LOGIN

        self.listener = await asyncssh.create_server(
            Peer, "127.0.0.1", 0, server_host_keys=[key], process_factory=self.run
        )

    @property
    def port(self):
        return self.listener.get_port()

    async def close(self):
        self.listener.close()
        await self.listener.wait_closed()

    def run(self, process):
        if process.term_type is not None:
            self.unexpected.append("pty")
        if process.command == _PROBE:
            self.calls.append("probe")
            if self.installed:
                process.stdout.write("/usr/bin/tmux\ntmux 3.3a\n")
                return process.exit(0)
            return process.exit(127)
        if process.command == _INSTALL:
            self.calls.append("install")
            if self.result == "install":
                self.installed = True
            return process.exit({"denied": 77, "unsupported": 69}.get(self.result, 0))
        self.unexpected.append("other-command")
        return process.exit(254)

    async def resolve(self, host, port):
        assert (host, port) == (HOST, self.port)
        return ("127.0.0.1",)

    def permit_loopback_fixture(self, target, ip):
        return (target.space_id, target.host, target.port, str(ip)) == (
            "primary",
            HOST,
            self.port,
            "127.0.0.1",
        )


def _app(tmp_path, monkeypatch, fixture):
    def factory(wiring):
        return NativeVPSReadiness(
            resolve_target=wiring.resolve_target,
            secret=wiring.secret,
            connector=connect_readiness,
            provisioner=provision_tmux,
            resolver=fixture.resolve,
        )

    # Only the loopback test tuple is redirected: the production egress
    # policy remains unchanged and no fixtures can connect outside loopback.
    monkeypatch.setattr(
        NativeVPSReadiness,
        "_allowed",
        lambda _, target, ip: fixture.permit_loopback_fixture(target, ip),
    )
    monkeypatch.setattr(
        wiring_fixture,
        "create_app",
        lambda data, **_: create_app(data, readiness_executor_factory=factory),
    )
    return wiring_fixture.prepare(tmp_path, monkeypatch, None)


@pytest.mark.parametrize(
    "result,ended,reason",
    [
        ("install", "ready", None),
        ("denied", "failed", "VPS_PRIVILEGE_REQUIRED"),
        ("unsupported", "failed", "VPS_ENV_UNSUPPORTED"),
        ("false_success", "failed", "INSTALL_OUTCOME_UNCONFIRMED"),
        ("wrong_pin", "failed", "HOST_KEY_CHANGED"),
        ("wrong_credential", "failed", "SSH_AUTH_FAILED"),
    ],
)
def test_manual_scan_and_install_run_real_pinned_ssh(tmp_path, monkeypatch, result, ended, reason):
    asyncssh = pytest.importorskip("asyncssh")
    key = asyncssh.generate_private_key("ssh-ed25519")
    saved_key = asyncssh.generate_private_key("ssh-ed25519") if result == "wrong_pin" else key
    fixture = LoopbackScenario(result)
    app = _app(tmp_path, monkeypatch, fixture)
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(fixture.start, asyncssh, key)
        try:
            owner = wiring_fixture.identity(client)
            saved = owner.post(
                "/api/vps",
                json={
                    "name": "real-fixture",
                    "host": HOST,
                    "port": fixture.port,
                    "username": "fixture",
                    "password": "incorrect-test-password"
                    if result == "wrong_credential"
                    else LOGIN,
                    "project_ids": [],
                    "host_key_policy": "confirm-change",
                    "host_key_fingerprint": saved_key.get_fingerprint("sha256"),
                },
            )
            assert saved.status_code == 200, saved.text
            vps = saved.json()["id"]
            scan, install = "/api/vps/readiness/scan", "/api/vps/readiness/install"
            assert owner.post(install).json() == {"queued_count": 0}
            assert owner.post(scan).json()["queued_count"] == 1
            if result in {"wrong_pin", "wrong_credential"}:
                rejected = wiring_fixture.wait_job(owner, vps, state="failed")
                assert rejected["reason_code"] == reason
                assert fixture.auth_count == (result == "wrong_credential") and fixture.calls == []
                assert owner.post(install).json() == {"queued_count": 0}
                return
            previous = wiring_fixture.wait_job(owner, vps, state="missing")
            assert previous["reason_code"] == "TMUX_MISSING"
            assert fixture.calls == ["probe"]
            assert owner.post(install).json()["queued_count"] == 1
            final = wiring_fixture.wait_job(owner, vps, state=ended)
            assert final["id"] == previous["id"]
            if reason:
                assert final["reason_code"] == reason
            assert fixture.calls.count("install") == 1
            assert fixture.calls[-1] == "probe"
            assert fixture.auth_count == 4
            assert fixture.unexpected == []
            assert owner.post(install).json() == {"queued_count": 0}
        finally:
            client.portal.call(fixture.close)
