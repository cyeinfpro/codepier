"""Real application/Store/IAM route integration with disposable ASGI peers.

Only the remote SSH endpoint is replaced.  HTTP authentication, authorization,
CSRF, WebSocket routing, ticket consumption and Agent channel cryptography are
the production implementations mounted by create_app.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from anyio import CancelScope
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hub.app import create_app
from hub.runtime import Connection
from hub.ssh_terminal import api as terminal_routes
from hub.ssh_terminal.service import TerminalService
from shared.crypto import password_hash
from shared.ssh_terminal_channel import terminal_secure_channel
from shared.ssh_terminal_config import TerminalConfig
from shared.ssh_terminal_protocol import TerminalProtocolError
from tests.legacy_iam_fixture import seed_owner


ORIGIN = "https://testserver"
DEVICE = "dd" * 16
PROJECT = "aa" * 16
OTHER_DEVICE = "ee" * 16
OTHER_PROJECT = "bb" * 16
GENERATION = "cc" * 16
DEVICE_SECRET = "synthetic-terminal-api-device-secret-" + "s" * 32
LOGIN_PASSWORD = "synthetic-terminal-api-login-password"
VPS_PASSWORD = "synthetic-terminal-api-vps-password"


@dataclass
class BrowserIdentity:
    client: TestClient
    cookie: str
    csrf: str
    space: str = "terminal-team"

    def headers(self, **changes):
        return {
            "Cookie": "rd_session=" + self.cookie,
            "X-RD-CSRF": self.csrf,
            "X-CodePier-Space": self.space,
            "Origin": ORIGIN,
            **changes,
        }

    def request(self, method, path, **kwargs):
        headers = self.headers(**kwargs.pop("headers", {}))
        return self.client.request(method, path, headers=headers, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)


class MainSocket:
    async def close(self, **kwargs):
        pass

    async def send_text(self, value):
        pass


class TerminalPeer:
    """Protocol-compatible peer which never opens a network or SSH channel."""

    def __init__(self):
        self.device_id, self.generation = DEVICE, GENERATION
        self.closed = False
        self.sessions, self.actions = {}, []
        self.revision = 0

    async def request(self, action, payload, project, **kwargs):
        self.actions.append(action)
        sid = payload["session_id"]
        if action == "reserve_create":
            return {"admission_token": None}
        if action == "create":
            self.sessions[sid] = {
                "session_id": sid,
                "runtime_generation": self.generation,
                "state": "ready",
                "mode": "direct",
                "stream_epoch": "1",
                "writer_epoch": "0",
                "cols": payload["cols"],
                "rows": payload["rows"],
                "available_start": "0",
                "available_end": "0",
                "ssh_connected": True,
                "recoverability": "resume",
            }
        if sid not in self.sessions:
            raise TerminalProtocolError("SESSION_NOT_FOUND", "Synthetic peer has no session")
        result = self.sessions[sid]
        if action == "attach":
            result.update(
                state="ready",
                writer_epoch=payload["writer_epoch"],
                resume_strategy="live",
                start_offset="0",
                output_end_offset="0",
                next_input_seq="1",
            )
        elif action == "detach":
            result["state"] = "detached"
        elif action in {"close", "revoke"}:
            result.update(
                state="exited", reason_code="USER_CLOSED" if action == "close" else "AUTH_REVOKED"
            )
        self.revision += 1
        return {**result, "state_revision": str(self.revision)}

    async def acknowledge(self, payload):
        pass

    async def input(self, frame):
        pass

    async def close(self):
        self.closed = True


class TerminalAPI:
    def __init__(self, app, client, identities, vps, role):
        self.app, self.client, self.identities = app, client, identities
        self.owner, self.alice, self.bob = (identities[key] for key in ("owner", "alice", "bob"))
        self.store, self.runtime = app.state.store, app.state.runtime
        self.service, self.vps, self.role = self.runtime.ssh_terminal, vps, role
        self.peer = None

    def install_main(self):
        connection = Connection(MainSocket(), None)
        connection.device_secret = self.store.one(
            "SELECT secret FROM devices WHERE id=?", (DEVICE,)
        )["secret"]
        self.runtime.connections[DEVICE] = connection
        return connection

    def install_terminal(self):
        if self.peer is None:
            self.install_main()
            self.peer = TerminalPeer()
            self.client.portal.call(self.service.register_agent, self.peer)
        return self.peer

    def body(self, **changes):
        return {
            "project_id": PROJECT,
            "vps_id": self.vps["id"],
            "request_id": uuid.uuid4().hex,
            "mode": "direct",
            **changes,
        }

    def create(self, identity=None):
        self.install_terminal()
        identity = identity or self.alice
        response = identity.post("/api/vps-terminal/sessions", json=self.body())
        assert response.status_code == 200, response.text
        sid = response.json()["session_id"]
        for _ in range(20):
            current = identity.get("/api/vps-terminal/sessions/" + sid)
            assert current.status_code == 200, current.text
            if current.json()["state"] == "ready":
                return current.json()
        raise AssertionError("Synthetic Agent did not become ready")

    def ticket(self, sid, identity=None, renderer=None):
        renderer = renderer or uuid.uuid4().hex
        response = (identity or self.alice).post(
            f"/api/vps-terminal/sessions/{sid}/attach-ticket", json={"renderer_id": renderer}
        )
        assert response.status_code == 200, response.text
        return {
            "type": "authenticate",
            "ticket": response.json()["ticket"],
            "renderer_id": renderer,
            "protocol_version": 1,
        }

    def capabilities(self, identity=None):
        return (identity or self.alice).get(
            "/api/vps-terminal/capabilities",
            params={
                "project_id": PROJECT,
                "vps_id": self.vps["id"],
            },
        )

    def permissions(self, actions):
        body = {
            key: self.role[key] for key in ("label", "enabled", "project_rules", "device_rules")
        }
        body.update(
            expected_version=self.role["version"],
            project_rules=[{"actions": actions, "projects": [PROJECT]}] if actions else [],
        )
        response = self.owner.put("/api/access-roles/" + self.role["id"], json=body)
        assert response.status_code == 200, response.text
        self.role = response.json()


@pytest.fixture
def terminal_api(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    monkeypatch.setenv("CODEPIER_SSH_TERMINAL_ENABLED", "1")
    app = create_app(str(tmp_path / "hub"))
    store = app.state.store
    hashed = password_hash(LOGIN_PASSWORD)
    for user in ("owner", "alice", "bob"):
        seed_owner(store, user, user)
        store.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, user))
    with store.transaction():
        for space in ("terminal-team", "terminal-other"):
            store.db.execute(
                "INSERT INTO spaces(id,label,kind,created) VALUES(?,?,'team',?)",
                (space, space, time.time()),
            )
            for user in ("owner", "alice", "bob"):
                store.db.execute(
                    "INSERT INTO memberships(space_id,user_id,level) VALUES(?,?,?)",
                    (space, user, "owner" if user == "owner" else "member"),
                )
        for device, project, space in (
            (DEVICE, PROJECT, "terminal-team"),
            (OTHER_DEVICE, OTHER_PROJECT, "terminal-other"),
        ):
            store.db.execute(
                "INSERT INTO devices(id,name,secret,space_id,owner_user_id,created) VALUES(?,?,?,?,'owner',?)",
                (device, "Synthetic Agent", store.encrypt(DEVICE_SECRET), space, time.time()),
            )
            store.db.execute(
                "INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,space_id,owner_user_id,created) "
                "VALUES(?,?,?,?,?,'write',1,?,'owner',?)",
                (
                    project,
                    "Terminal fixture",
                    "terminal fixture",
                    device,
                    str(tmp_path / project),
                    space,
                    time.time(),
                ),
            )
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=True) as client:
        identities = {}
        for user in ("owner", "alice", "bob"):
            response = client.post(
                "/api/login",
                json={"username": user, "password": LOGIN_PASSWORD},
                headers={"Origin": ORIGIN},
            )
            assert response.status_code == 200, response.text
            identities[user] = BrowserIdentity(
                client, response.cookies["rd_session"], response.json()["csrf"]
            )
            client.cookies.clear()
        owner = identities["owner"]
        saved = owner.post(
            "/api/vps",
            json={
                "name": "Synthetic VPS",
                "host": "fixture.invalid",
                "username": "fixture",
                "password": VPS_PASSWORD,
                "project_ids": [PROJECT],
                "host_key_policy": "strict",
            },
        )
        assert saved.status_code == 200, saved.text
        role = owner.post(
            "/api/access-roles",
            json={
                "label": "Terminal fixture role",
                "project_rules": [{"actions": ["read", "write", "execute"], "projects": [PROJECT]}],
                "device_rules": [],
                "idempotency_key": uuid.uuid4().hex,
            },
        )
        assert role.status_code == 201, role.text
        for user in ("alice", "bob"):
            assigned = owner.put(
                f"/api/iam/spaces/terminal-team/assignments/{role.json()['id']}/{user}",
                json={"active": True, "may_delegate": False, "expected_version": 0},
            )
            assert assigned.status_code == 200, assigned.text
        yield TerminalAPI(app, client, identities, saved.json(), role.json())


def browser_socket(api, identity=None, **header_changes):
    return api.client.websocket_connect(
        "wss://testserver/ws/vps-terminal",
        headers=(identity or api.alice).headers(**header_changes),
    )


def rejected_socket(socket):
    """A rejected first frame may have one safe JSON error before its close."""
    message = socket.receive()
    reason = None
    if message["type"] == "websocket.send":
        body = json.loads(message["text"])
        assert body["type"] == "error"
        reason = body["reason_code"]
        message = socket.receive()
    assert message["type"] == "websocket.close", message
    return reason, message.get("code")


def agent_hello(api, socket, *, secret=DEVICE_SECRET, **changes):
    challenge = socket.receive_json()
    assert challenge["type"] == "terminal_challenge" and challenge["protocol"] == 1
    assert len(challenge["challenge"]) >= 32
    peer = terminal_secure_channel(secret, challenge["challenge"], DEVICE, "agent")
    hello = {
        "type": "terminal_hello",
        "protocol": 1,
        "runtime_generation": GENERATION,
        "limits": {
            key: getattr(api.service.config, key)
            for key in ("frame_bytes", "input_frame_bytes", "control_bytes")
        },
        **changes,
    }
    socket.send_text(peer.pack(hello))
    return peer


def test_routes_are_mounted_and_require_a_panel_login(terminal_api):
    api = terminal_api
    response = api.client.get("/api/vps-terminal/sessions")
    assert response.status_code == 401 and response.json()["error"]["code"] == "LOGIN_REQUIRED"
    response = api.alice.get("/api/vps-terminal/sessions")
    assert response.status_code == 200 and response.json() == {"sessions": []}


def test_diagnostics_returns_only_authorized_safe_measurements_and_a_local_status_probe(
    terminal_api,
):
    api = terminal_api
    session = api.create()
    sid = session["session_id"]
    secret = "diagnostic-secret-never-return"
    api.peer.sessions[sid].update(
        stage_timings_ms={"ssh_connect": 12, "pty_open": 0, "private_command": secret},
        agent_version="1.33.37",
        protocol_version=1,
        bytes_in=3,
        bytes_out=19,
        ring_bytes=19,
        output_outstanding_bytes=0,
        backpressure_count=2,
        backpressure_duration_ms=7,
        password=secret,
        responses=[secret],
        ticket=secret,
        output=secret,
    )
    before = len(api.peer.actions)
    response = api.alice.get(f"/api/vps-terminal/sessions/{sid}/diagnostics")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert api.peer.actions[before:] == ["status"]
    assert result["session_id"] == sid
    assert result["links"]["browser"]["status"] == "detached"
    assert result["links"]["agent"]["status"] == "connected"
    assert result["links"]["agent"]["control_rtt_ms"] >= 0
    assert result["links"]["ssh"]["connected"] is True
    assert result["bytes"] == {"input": "3", "output": "19"}
    assert result["stages_ms"]["ssh_connect"] == 12 and result["stages_ms"]["pty_open"] == 0
    assert set(result["stages_ms"]) <= {"ssh_connect", "pty_open", "hub_prepare", "attach"}
    assert result["metrics"]["gauges"]["agent_queue_bytes"] is None
    assert result["metrics"]["gauges"]["ssh_ring_bytes"] == 19
    assert result["metrics"]["counters"]["backpressure_total"] == 2
    assert result["lifecycle"]["version"]["agent"] == "1.33.37"
    for forbidden in (
        secret,
        VPS_PASSWORD,
        DEVICE_SECRET,
        "fixture.invalid",
        "project_root",
        "responses",
        "ticket",
    ):
        assert forbidden not in response.text
    persisted = api.store.one("SELECT metadata FROM vps_terminal_sessions WHERE id=?", (sid,))[
        "metadata"
    ]
    assert secret not in persisted
    repeated = api.alice.get(f"/api/vps-terminal/sessions/{sid}/diagnostics")
    assert repeated.status_code == 429 and repeated.json()["error"]["code"] == "DIAGNOSTIC_BUSY"


def test_diagnostics_requires_current_login_owner_space_and_execution_permission(terminal_api):
    api = terminal_api
    sid = api.create()["session_id"]
    path = f"/api/vps-terminal/sessions/{sid}/diagnostics"
    before = list(api.peer.actions)
    assert api.client.get(path).status_code == 401
    assert api.bob.get(path).status_code == 404
    assert api.owner.get(path).status_code == 404
    assert api.alice.get(path, headers={"X-CodePier-Space": "terminal-other"}).status_code == 404
    api.permissions(["read", "write"])
    assert api.alice.get(path).status_code == 403
    assert api.peer.actions == before


def test_terminal_aggregate_metrics_require_instance_admin_and_have_no_identity_labels(
    terminal_api,
):
    api = terminal_api
    sid = api.create()["session_id"]
    assert api.client.get("/api/vps-terminal/metrics").status_code == 401
    denied = api.alice.get("/api/vps-terminal/metrics")
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "INSTANCE_ADMIN_REQUIRED"
    response = api.owner.get("/api/vps-terminal/metrics")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    snapshot = response.json()
    assert snapshot["scope"] == "hub_process"
    assert "agent_control_rtt_ms" in snapshot["histograms"]
    for forbidden in (sid, DEVICE, PROJECT, VPS_PASSWORD, "fixture.invalid", "alice"):
        assert forbidden not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {"X-RD-CSRF": ""},
        {"X-RD-CSRF": "wrong"},
        {"Origin": "https://evil.invalid"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_create_rejects_missing_csrf_and_cross_site_requests(terminal_api, headers):
    api = terminal_api
    api.install_terminal()
    response = api.alice.post("/api/vps-terminal/sessions", json=api.body(), headers=headers)
    assert response.status_code == 403, response.text
    assert api.peer.actions == []
    assert api.store.all("SELECT id FROM vps_terminal_sessions") == []


@pytest.mark.parametrize(
    "actions", [[], ["read"], ["read", "write"], ["read", "execute"], ["read", "write", "execute"]]
)
def test_terminal_requires_read_write_and_execute_on_the_same_project(terminal_api, actions):
    api = terminal_api
    api.install_terminal()
    api.permissions(actions)
    response = api.alice.post("/api/vps-terminal/sessions", json=api.body())
    if set(actions) == {"read", "write", "execute"}:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code in {403, 404}, response.text
        assert api.peer.actions == []
        assert api.store.all("SELECT id FROM vps_terminal_sessions") == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "attacker.invalid"),
        ("port", 2222),
        ("username", "root"),
        ("password", "do-not-reflect-password"),
        ("private_key", "do-not-reflect-key"),
        ("command", "arbitrary command"),
        ("device_id", OTHER_DEVICE),
        ("space_id", "terminal-other"),
        ("owner_user_id", "bob"),
    ],
)
def test_browser_cannot_supply_target_credentials_command_or_ownership(terminal_api, field, value):
    api = terminal_api
    api.install_terminal()
    response = api.alice.post("/api/vps-terminal/sessions", json=api.body(**{field: value}))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_ARGUMENTS"
    assert api.peer.actions == []
    assert "do-not-reflect" not in response.text


def test_capabilities_distinguish_offline_old_agent_and_supported_agent(terminal_api):
    api = terminal_api
    offline = api.capabilities()
    assert offline.status_code == 200, offline.text
    assert not offline.json()["available"] and offline.json()["reason_code"] == "AGENT_OFFLINE"
    api.install_main()
    legacy = api.capabilities().json()
    assert not legacy["available"] and legacy["reason_code"] == "AGENT_UPGRADE_REQUIRED"
    api.install_terminal()
    current = api.capabilities().json()
    assert current["available"] and current["protocol_version"] == 1
    assert current["runtime_generation"] == GENERATION
    assert current["limits"]["frame_bytes"] == api.service.config.frame_bytes
    assert VPS_PASSWORD not in json.dumps(current)


def test_sessions_remain_private_to_owner_and_selected_space(terminal_api):
    api = terminal_api
    session = api.create()
    sid = session["session_id"]
    for identity in (api.bob, api.owner):
        assert identity.get(f"/api/vps-terminal/sessions/{sid}").status_code == 404
        assert (
            identity.post(
                f"/api/vps-terminal/sessions/{sid}/attach-ticket",
                json={"renderer_id": uuid.uuid4().hex},
            ).status_code
            == 404
        )
        assert sid not in identity.get("/api/vps-terminal/sessions").text
    wrong_space = api.alice.get(
        f"/api/vps-terminal/sessions/{sid}", headers={"X-CodePier-Space": "terminal-other"}
    )
    assert wrong_space.status_code in {403, 404}
    assert VPS_PASSWORD not in json.dumps(session)
    assert VPS_PASSWORD not in json.dumps(api.store.all("SELECT * FROM vps_terminal_sessions"))
    assert VPS_PASSWORD not in json.dumps(api.store.all("SELECT * FROM audit"))


@pytest.mark.parametrize("action", ["attach-ticket", "close", "detach", "takeover"])
def test_attachment_and_lifecycle_mutations_require_csrf(terminal_api, action):
    api = terminal_api
    sid = api.create()["session_id"]
    body = (
        {"renderer_id": uuid.uuid4().hex}
        if action == "attach-ticket"
        else {"request_id": uuid.uuid4().hex}
    )
    response = api.alice.post(
        f"/api/vps-terminal/sessions/{sid}/{action}", json=body, headers={"X-RD-CSRF": ""}
    )
    assert response.status_code == 403


def test_https_origin_matches_wss_and_ticket_is_one_use(terminal_api):
    api = terminal_api
    sid = api.create()["session_id"]
    authenticate = api.ticket(sid)
    with browser_socket(api) as socket:
        socket.send_json(authenticate)
        attached = socket.receive_json()
        assert attached["type"] == "attached" and attached["session_id"] == sid
        assert attached["runtime_generation"] == GENERATION
        with browser_socket(api) as duplicate:
            duplicate.send_json(authenticate)
            reason, code = rejected_socket(duplicate)
            assert reason == "ATTACH_TICKET_INVALID" and code == 4401


@pytest.mark.parametrize(
    "origin", ["https://evil.invalid", "null", "http://testserver", "https://testserver:444"]
)
def test_browser_origin_is_rejected_before_accept(terminal_api, origin):
    with pytest.raises(WebSocketDisconnect), browser_socket(terminal_api, Origin=origin):
        pytest.fail("Cross-origin browser WebSocket was accepted")


def test_unauthenticated_browser_is_rejected_before_accept(terminal_api):
    with pytest.raises(WebSocketDisconnect), browser_socket(terminal_api, Cookie=""):
        pytest.fail("Browser without a panel cookie was accepted")


def test_half_authenticated_browser_socket_quotas_release_on_every_exit(terminal_api):
    api = terminal_api
    api.service.config = replace(
        api.service.config, max_sessions_per_user=1, max_sessions_per_hub=2
    )
    with ExitStack() as sockets:
        sockets.enter_context(browser_socket(api, api.alice))
        sockets.enter_context(browser_socket(api, api.alice))
        with pytest.raises(WebSocketDisconnect), browser_socket(api, api.alice):
            pytest.fail("Per-user half-authentication quota was bypassed")
        assert len(api.service.browser_socket_slots) == 2
        sockets.enter_context(browser_socket(api, api.bob))
        sockets.enter_context(browser_socket(api, api.bob))
        with pytest.raises(WebSocketDisconnect), browser_socket(api, api.owner):
            pytest.fail("Global half-authentication quota was bypassed")
        assert len(api.service.browser_socket_slots) == 4
    assert api.service.browser_socket_slots == {}
    # Refusals and clients disappearing before authenticate must both release
    # their reservations; a subsequent legitimate reconnect still works.
    with browser_socket(api, api.alice):
        assert len(api.service.browser_socket_slots) == 1
    assert api.service.browser_socket_slots == {}


def test_binary_first_frame_is_closed_without_an_asgi_exception(terminal_api):
    with browser_socket(terminal_api) as socket:
        socket.send_bytes(b"not-a-json-authenticate-frame")
        _reason, code = rejected_socket(socket)
        assert code in {1003, 4401, 4403}


def test_disconnect_before_authenticate_is_normal_cleanup(terminal_api):
    with browser_socket(terminal_api):
        pass


def test_ticket_cannot_be_transferred_to_another_user_or_login(terminal_api):
    api = terminal_api
    sid = api.create()["session_id"]
    for identity in (api.bob, api.owner):
        authenticate = api.ticket(sid)
        with browser_socket(api, identity) as socket:
            socket.send_json(authenticate)
            reason, code = rejected_socket(socket)
            assert reason == "ATTACH_TICKET_INVALID" and code == 4401
    authenticate = api.ticket(sid)
    with api.store.transaction():
        fresh = api.app.state.auth.new_session("alice")
    another_login = BrowserIdentity(api.client, fresh["cookie"], fresh["csrf"])
    with browser_socket(api, another_login) as socket:
        socket.send_json(authenticate)
        reason, code = rejected_socket(socket)
        assert reason == "ATTACH_TICKET_INVALID" and code == 4401
    assert "attach" not in api.peer.actions


@pytest.mark.parametrize("change", ["role", "device", "target_revision"])
def test_ticket_rechecks_current_authority_and_target_before_attachment(terminal_api, change):
    api = terminal_api
    sid = api.create()["session_id"]
    authenticate = api.ticket(sid)
    if change == "role":
        api.store.execute("UPDATE role_assignments SET active=0 WHERE user_id='alice'")
    elif change == "device":
        api.store.execute("UPDATE devices SET enabled=0 WHERE id=?", (DEVICE,))
    else:
        api.store.execute(
            "UPDATE vps_connections SET connection_revision=connection_revision+1 WHERE id=?",
            (api.vps["id"],),
        )
    with browser_socket(api) as socket:
        socket.send_json(authenticate)
        reason, code = rejected_socket(socket)
        assert reason and code in {4401, 4403}
    assert "attach" not in api.peer.actions


def test_invalid_session_path_is_a_client_error(terminal_api):
    response = terminal_api.alice.get("/api/vps-terminal/sessions/not-a-uuid")
    assert response.status_code in {400, 404, 422}, response.text


@pytest.mark.parametrize("change", ["no_main", "disabled", "rotated"])
def test_agent_requires_current_main_identity_before_sending_a_challenge(terminal_api, change):
    api = terminal_api
    if change != "no_main":
        api.install_main()
    if change == "disabled":
        api.store.execute("UPDATE devices SET enabled=0 WHERE id=?", (DEVICE,))
    elif change == "rotated":
        api.store.execute(
            "UPDATE devices SET secret=? WHERE id=?", (api.store.encrypt("r" * 48), DEVICE)
        )
    with (
        pytest.raises(WebSocketDisconnect),
        api.client.websocket_connect(f"wss://testserver/agent/vps-terminal/{DEVICE}"),
    ):
        pytest.fail("Unauthorized Agent received an accepted connection")
    assert DEVICE not in api.service.agents


def test_agent_handshake_uses_real_separate_channel_and_registers_capability(terminal_api):
    api = terminal_api
    api.install_main()
    with api.client.websocket_connect(f"wss://testserver/agent/vps-terminal/{DEVICE}") as socket:
        peer = agent_hello(api, socket)
        ready = peer.unpack(socket.receive_text())
        assert ready == {"type": "terminal_ready", "protocol": 1, "runtime_generation": GENERATION}
        capabilities = api.capabilities().json()
        assert capabilities["available"] and capabilities["runtime_generation"] == GENERATION
        assert api.service.agents[DEVICE].main_connection is api.runtime.connections[DEVICE]
    assert DEVICE not in api.service.agents
    assert DEVICE_SECRET not in json.dumps(api.store.all("SELECT * FROM audit"))


def test_pending_agent_handshake_slots_are_bounded_and_released_after_authentication(terminal_api):
    api = terminal_api
    api.install_main()
    url = f"wss://testserver/agent/vps-terminal/{DEVICE}"
    with ExitStack() as sockets:
        for _ in range(2):
            socket = sockets.enter_context(api.client.websocket_connect(url))
            assert socket.receive_json()["type"] == "terminal_challenge"
        assert len(api.service.agent_handshake_slots) == 2
        with pytest.raises(WebSocketDisconnect), api.client.websocket_connect(url):
            pytest.fail("A third unauthenticated Agent socket bypassed its device quota")
    assert api.service.agent_handshake_slots == {}
    with api.client.websocket_connect(url) as socket:
        peer = agent_hello(api, socket)
        assert peer.unpack(socket.receive_text())["type"] == "terminal_ready"
        assert api.capabilities().json()["available"]
        assert api.service.agent_handshake_slots == {}


@pytest.mark.parametrize("change", ["rotate_secret", "disable_device", "replace_main"])
def test_agent_handshake_rechecks_authority_after_encrypted_hello(terminal_api, change):
    api = terminal_api
    api.install_main()
    with api.client.websocket_connect(f"wss://testserver/agent/vps-terminal/{DEVICE}") as socket:
        challenge = socket.receive_json()
        peer = terminal_secure_channel(DEVICE_SECRET, challenge["challenge"], DEVICE, "agent")
        if change == "rotate_secret":
            api.store.execute(
                "UPDATE devices SET secret=? WHERE id=?", (api.store.encrypt("r" * 48), DEVICE)
            )
        elif change == "disable_device":
            api.store.execute("UPDATE devices SET enabled=0 WHERE id=?", (DEVICE,))
        else:
            api.install_main()
        socket.send_text(
            peer.pack(
                {
                    "type": "terminal_hello",
                    "protocol": 1,
                    "runtime_generation": GENERATION,
                    "limits": {
                        key: getattr(api.service.config, key)
                        for key in ("frame_bytes", "input_frame_bytes", "control_bytes")
                    },
                }
            )
        )
        assert socket.receive()["type"] == "websocket.close"
    assert DEVICE not in api.service.agents


def test_wrong_agent_secret_never_reaches_ready(terminal_api):
    api = terminal_api
    api.install_main()
    with api.client.websocket_connect(f"wss://testserver/agent/vps-terminal/{DEVICE}") as socket:
        agent_hello(api, socket, secret="wrong-secret-" + "w" * 48)
        assert socket.receive()["type"] == "websocket.close"
    assert DEVICE not in api.service.agents


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_agent_scope_cancellation_settles_rpc_and_unregisters_only_its_identity(
    monkeypatch, replacement
):
    scope = CancelScope()
    pending = asyncio.get_running_loop().create_future()
    attachment = SimpleNamespace(shutdown=AsyncMock())
    next_connection = object()
    runtime = SimpleNamespace(connections={DEVICE: object()})
    watchdog = asyncio.Event()
    connection = None
    service = SimpleNamespace(
        agents={},
        sessions={"attached": SimpleNamespace(row={"device_id": DEVICE}, attachment=attachment)},
        config=TerminalConfig(),
        on_agent_event=AsyncMock(),
        reserve_agent_handshake=lambda device_id: device_id,
        release_agent_handshake=AsyncMock(),
        metrics=SimpleNamespace(increment=lambda metric: None),
    )
    released = []
    service.release_agent_handshake = released.append

    async def register_agent(current):
        nonlocal connection
        connection = current
        service.agents[DEVICE] = next_connection if replacement else current
        current.pending["admitted-status"] = pending
        scope.cancel()

    service.register_agent = register_agent
    service.unregister_agent = lambda current: TerminalService.unregister_agent(service, current)
    socket = SimpleNamespace(
        accept=AsyncMock(),
        send_json=AsyncMock(),
        send_text=AsyncMock(),
        receive_text=watchdog.wait,
        close=AsyncMock(),
    )
    store = SimpleNamespace(run=AsyncMock(return_value=DEVICE_SECRET))
    monkeypatch.setattr(terminal_routes, "_receive_agent_hello", AsyncMock(return_value=GENERATION))

    with scope:
        await terminal_routes._agent_socket(socket, DEVICE, runtime, store, service)
        pytest.fail("ownership cancellation must reach the owning scope")

    assert connection is not None and connection.closed
    assert released == [DEVICE, None]
    assert isinstance(pending.exception(), TerminalProtocolError)
    assert pending.exception().code == "AGENT_RESPONSE_UNKNOWN"
    socket.close.assert_awaited()
    if replacement:
        assert service.agents[DEVICE] is next_connection
        attachment.shutdown.assert_not_awaited()
    else:
        assert DEVICE not in service.agents
        attachment.shutdown.assert_awaited_once_with("AGENT_OFFLINE", 1012)
