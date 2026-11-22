"""Maintenance allows only authorized drain and retriable old transport reconciliation."""

from __future__ import annotations

import uuid

import pytest

from agent.ssh_terminal_manager import TerminalSessionManager
from shared.ssh_terminal_protocol import TerminalProtocolError
from tests.test_ssh_terminal_api import GENERATION, MainSocket, TerminalPeer
from tests.test_ssh_terminal_api import terminal_api as terminal_api


def _maintenance(api, tmp_path):
    gate = api.runtime.panel_maintenance
    gate.marker = tmp_path / "maintenance.json"
    gate.marker.write_text("{}")
    return gate


def _drain_ready(api):
    response = api.client.get("/healthz")
    assert response.status_code == 200
    return response.json()["panel_update"]["drain_ready"]


def test_owner_close_inside_maintenance_drains_without_admitting_other_work(terminal_api, tmp_path):
    api = terminal_api
    session = api.create()
    session_path = "/api/vps-terminal/sessions/" + session["id"]
    gate = _maintenance(api, tmp_path)
    assert not _drain_ready(api)

    for path, body in (
        ("/api/vps-terminal/sessions", api.body()),
        (session_path + "/attach-ticket", {"renderer_id": "44" * 16}),
        (session_path + "/takeover", {}),
        (session_path + "/detach", {}),
    ):
        response = api.alice.post(path, json=body)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "PANEL_UPDATING"

    close_path = session_path + "/close"
    denied = api.bob.post(close_path, json={"request_id": uuid.uuid4().hex})
    assert denied.status_code in {403, 404}
    assert (
        api.alice.post(
            close_path, json={"request_id": uuid.uuid4().hex}, headers={"X-RD-CSRF": ""}
        ).status_code
        == 403
    )
    assert (
        api.alice.post(
            close_path, json={"request_id": uuid.uuid4().hex}, headers={"Cookie": ""}
        ).status_code
        == 401
    )
    assert api.alice.get(close_path).status_code == 503
    assert "close" not in api.peer.actions
    assert not _drain_ready(api)

    request = api.peer.request

    async def check_counted(action, payload, project, **kwargs):
        if action == "close":
            assert gate.inflight == 1
            assert not gate.status()["drain_ready"]
        return await request(action, payload, project, **kwargs)

    api.peer.request = check_counted
    closed = api.alice.post(close_path, json={"request_id": uuid.uuid4().hex})
    assert closed.status_code == 200, closed.text
    assert (closed.json()["state"], closed.json()["reason_code"]) == ("exited", "USER_CLOSED")
    assert gate.inflight == 0
    assert _drain_ready(api)


class ReplacementPeer(TerminalPeer):
    def __init__(self, api, *, effect):
        super().__init__()
        self.api = api
        self.effect = effect
        self.manager = TerminalSessionManager(
            emit=lambda _event: None, authorize=lambda _project: None
        )
        self.generation = GENERATION if effect == "same_generation" else self.manager.generation
        self.main_connection = api.install_main()
        if effect == "unbound":
            self.main_connection = MainSocket()

    async def request(self, action, payload, project, **kwargs):
        self.actions.append(action)
        if action == "status":
            assert payload["runtime_generation"] == self.generation
            assert "password" not in payload
            if self.effect == "timeout":
                raise TimeoutError("fixture")
            if self.effect == "ambiguous":
                return {}
            if self.effect == "replacement_during_status":
                self.api.install_main()
            return await self.manager.control(action, payload, project)
        raise TerminalProtocolError("SESSION_NOT_FOUND", "fixture has no such legacy close target")


@pytest.mark.parametrize("mode", ["direct", "confirmed_auto"])
def test_replacement_definitive_missing_retires_only_known_direct_transport(
    terminal_api, tmp_path, mode
):
    api = terminal_api
    session = api.create()
    sid = session["id"]
    if mode == "confirmed_auto":
        api.store.execute(
            "UPDATE vps_terminal_sessions SET requested_mode='auto' WHERE id=?", (sid,)
        )
        cached = api.service.sessions[sid]
        cached.row = api.service.repository.get(sid)

        async def record_selected_direct():
            await api.service.update(cached, {"stage_timings_ms": {"pty_open": 3.0}})

        api.client.portal.call(record_selected_direct)
    replacement = ReplacementPeer(api, effect="missing")
    api.client.portal.call(api.service.register_agent, replacement)
    _maintenance(api, tmp_path)
    assert not _drain_ready(api)

    close_path = f"/api/vps-terminal/sessions/{sid}/close"
    response = api.alice.post(close_path, json={"request_id": uuid.uuid4().hex})
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["state"] == "failed"
    assert state["reason_code"] == "SESSION_NOT_RECOVERABLE"
    assert state["runtime_generation"] == GENERATION  # The old record is not rebound.
    assert state["mode"] == "direct"
    assert state["remote_session_state"] == "unknown"
    assert state["output_complete"] is False and state["ssh_connected"] is False
    assert replacement.actions == ["status"]
    assert _drain_ready(api)
    assert (
        api.alice.post(close_path, json={"request_id": uuid.uuid4().hex}).json()["reason_code"]
        == state["reason_code"]
    )
    assert replacement.actions == ["status"]


@pytest.mark.parametrize(
    ("effect", "mode", "expected_actions"),
    [
        ("same_generation", "direct", ["close"]),
        ("timeout", "direct", ["status"]),
        ("ambiguous", "direct", ["status"]),
        ("unbound", "direct", []),
        ("replacement_during_status", "direct", ["status"]),
        ("missing", "tmux", ["close"]),
        ("missing", "provisional_auto", ["close"]),
    ],
)
def test_missing_or_unknown_without_definitive_direct_replacement_stays_busy(
    terminal_api, tmp_path, effect, mode, expected_actions
):
    api = terminal_api
    session = api.create()
    sid = session["id"]
    cached = api.service.sessions[sid]
    if mode == "tmux":

        async def mark_tmux():
            await api.service.update(cached, {"mode": "tmux", "remote_session_state": "unknown"})

        api.client.portal.call(mark_tmux)
    if mode == "provisional_auto":
        api.store.execute(
            "UPDATE vps_terminal_sessions SET requested_mode='auto' WHERE id=?", (sid,)
        )
        cached.row = api.service.repository.get(sid)
    replacement = ReplacementPeer(api, effect=effect)
    api.client.portal.call(api.service.register_agent, replacement)
    _maintenance(api, tmp_path)
    response = api.alice.post(
        f"/api/vps-terminal/sessions/{sid}/close", json={"request_id": uuid.uuid4().hex}
    )
    assert response.status_code == 200, response.text
    state = response.json()
    assert (state["state"], state["reason_code"]) == ("close_unknown", "CLOSE_UNKNOWN")
    assert replacement.actions == expected_actions
    assert not _drain_ready(api)
