"""Regression cases for API trust boundaries and credential lifecycle races."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import hub.app as app_module
import hub.auth as auth_module
from hub.app import BodyLimit, create_app
from hub.auth import Auth
from hub.runtime import Principal, Runtime
from hub.store import Store
from shared.crypto import digest, password_hash
from shared.instance_lock import InstanceLock
from shared.util import DevError


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    app = create_app(str(tmp_path / "hub"))
    store = app.state.store
    store.execute("INSERT INTO users VALUES (?,?,?,?)", ("owner", "admin", password_hash("original-password"), time.time()))
    store.execute("INSERT INTO sessions VALUES (?,?,?,?)", (digest("session"), "owner", "csrf", time.time() + 3600))
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)", ("device", "fixture", store.encrypt("x" * 43), time.time()))
    store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)", ("project", "fixture", "fixture", "device", "/tmp/fixture", "", "write", 0, time.time()))
    principal = Principal("panel:admin", "owner", {"read", "write"}, ["*"], admin=True)
    pat = app.state.auth.issue_grant(principal, "fixture", ["read"], ["project"])["token"]
    with TestClient(app) as client:
        client.cookies.set("rd_session", "session")
        client.headers["X-RD-CSRF"] = "csrf"
        yield app, client, pat


def rpc(client, pat, payload):
    return client.post("/mcp", content=json.dumps(payload), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + pat,
    })


def oauth_tokens(client):
    registration = client.post("/oauth/register", json={"redirect_uris": ["http://localhost:12345/callback"]}).json()
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": registration["client_id"], "redirect_uri": "http://localhost:12345/callback", "code_challenge_method": "S256", "code_challenge": challenge, "resource": "http://testserver/mcp"}
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    decision = client.post(f"/api/oauth/requests/{request_id}/decide", json={"allow": True, "scopes": ["read"], "projects": ["project"]})
    assert decision.status_code == 200, decision.text
    code = parse_qs(urlsplit(decision.json()["redirect"]).query)["code"][0]
    tokens = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": registration["client_id"], "redirect_uri": params["redirect_uri"], "code_verifier": verifier})
    assert tokens.status_code == 200, tokens.text
    return registration["client_id"], tokens.json()


@pytest.mark.parametrize("value", [[], {}, 1, None, True])
def test_mcp_initialize_rejects_non_string_version(api, value):
    app, client, pat = api
    response = rpc(client, pat, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": value}})
    assert response.json()["error"]["code"] == -32602
    assert rpc(client, pat, {"jsonrpc": "2.0", "id": 2, "method": "ping"}).json()["result"] == {}


@pytest.mark.parametrize("raw", [
    '{"jsonrpc":"2.0","id":' + "9" * 5000 + ',"method":"ping"}',
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}',
    '{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}',
    "[" * 1100 + "]" * 1100,
])
def test_mcp_malformed_json_never_becomes_server_error(api, raw):
    app, client, pat = api
    response = client.post("/mcp", content=raw, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer " + pat})
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {-32600, -32700}
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path,body", [
    ("/api/login", {"username": "\ud800", "password": "password"}),
    ("/api/devices", {"name": "\ud800", "hub_url": "http://testserver"}),
    ("/api/tools/call", {"tool": "projects_list", "arguments": {"value": float("inf")}}),
    ("/api/tools/call", {"tool": "projects_list", "arguments": {"value": "\ud800"}}),
])
def test_panel_rejects_non_roundtrippable_json(api, path, body):
    _, client, _ = api
    response = client.post(path, content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("raw", ['{"client_name":"\\ud800","redirect_uris":["http://localhost/callback"]}', '{"number":' + "9" * 5000 + '}', "[" * 1100 + "]" * 1100])
def test_oauth_rejects_non_roundtrippable_json(api, raw):
    _, client, _ = api
    assert client.post("/oauth/register", content=raw, headers={"Content-Type": "application/json"}).status_code == 400


@pytest.mark.parametrize("redirect", ["\nhttps://chatgpt.com/callback", "https://chatgpt.com/\tcallback", "https://chatgpt.com/\\callback", "https://@chatgpt.com/callback", "http://localhost:0/callback", "https://chatgpt.com/callback#"])
def test_oauth_redirect_rejects_browser_normalized_addresses(api, redirect):
    _, client, _ = api
    assert client.post("/oauth/register", json={"redirect_uris": [redirect]}).status_code == 400


def test_csrf_non_ascii_header_is_rejected(api):
    _, client, _ = api
    response = client.post("/api/logout", headers={b"x-rd-csrf": b"\xe9"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"


def test_bearer_scheme_is_case_insensitive(api):
    _, client, pat = api
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": "bEaReR " + pat, "Accept": "application/json, text/event-stream"})
    assert response.json()["result"] == {}


@pytest.mark.parametrize("path", ["/api/operations", "/api/audit", "/api/audit-export"])
def test_pagination_rejects_offsets_outside_sqlite_range(api, path):
    _, client, _ = api
    assert client.get(path, params={"offset": 2**100}).status_code == 422


def test_chunked_body_limit_is_413_before_endpoint_side_effects():
    async def scenario():
        calls, sent = [], []
        async def app(scope, receive, send):
            calls.append(True)
        chunks = iter([
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": True},
            {"type": "http.request", "body": b"90", "more_body": False},
        ])
        async def receive():
            return next(chunks)
        async def send(message):
            sent.append(message)
        await BodyLimit(app, limit=8)({"type": "http", "headers": []}, receive, send)
        assert not calls
        assert sent[0]["status"] == 413
    asyncio.run(scenario())


def test_chunked_valid_body_is_delivered_intact():
    async def scenario():
        received = []
        async def app(scope, receive, send):
            received.append(await receive())
            received.append(await receive())
        chunks = iter([
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
            {"type": "http.disconnect"},
        ])
        async def receive():
            return next(chunks)
        async def send(message):
            pass
        await BodyLimit(app, limit=8)({"type": "http", "headers": []}, receive, send)
        assert received == [{"type": "http.request", "body": b"123456", "more_body": False}, {"type": "http.disconnect"}]
    asyncio.run(scenario())


def test_login_cannot_issue_session_after_password_reset(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    try:
        auth = Auth(store)
        store.execute("INSERT INTO users VALUES (?,?,?,?)", ("owner", "admin", "old-hash", time.time()))
        started, release = threading.Event(), threading.Event()
        def verify(password, encoded):
            started.set()
            assert release.wait(5)
            return True
        monkeypatch.setattr(auth_module, "password_verify", verify)
        async def scenario():
            request = Request({"type": "http", "method": "POST", "scheme": "http", "server": ("localhost", 80), "path": "/api/login", "headers": [], "client": ("test", 1)})
            login = asyncio.create_task(auth.login(request, "admin", "old-password"))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                store.execute("UPDATE users SET password_hash='new-hash' WHERE id='owner'")
            finally:
                release.set()
            with pytest.raises(DevError, match="密码已变化"):
                await login
            assert store.one("SELECT count(*) AS n FROM sessions")["n"] == 0
        asyncio.run(scenario())
    finally:
        store.close()


def test_cancelled_login_keeps_hash_capacity_until_thread_finishes(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    try:
        auth = Auth(store)
        entered, release = threading.Event(), threading.Event()
        def verify(password, encoded):
            entered.set()
            assert release.wait(5)
            return False
        monkeypatch.setattr(auth_module, "password_verify", verify)
        async def scenario():
            request = Request({"type": "http", "method": "POST", "scheme": "http", "server": ("localhost", 80), "path": "/api/login", "headers": [], "client": ("test", 1)})
            login = asyncio.create_task(auth.login(request, "unknown", "password"))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                login.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await login
                assert auth.login_inflight == 1
                auth.login_inflight = 4
                with pytest.raises(DevError) as exc:
                    await auth.login(request, "unknown", "password")
                assert exc.value.status == 429
                auth.login_inflight = 1
            finally:
                release.set()
            for _ in range(100):
                if auth.login_inflight == 0:
                    break
                await asyncio.sleep(.01)
            assert auth.login_inflight == 0
        asyncio.run(scenario())
    finally:
        store.close()


def test_password_change_rechecks_session_after_async_verification(api, monkeypatch):
    app, client, _ = api
    store = app.state.store
    def verify(password, encoded):
        store.execute("DELETE FROM sessions")
        return True
    monkeypatch.setattr(app_module, "password_verify", verify)
    old_hash = store.one("SELECT password_hash FROM users WHERE id='owner'")["password_hash"]
    response = client.post("/api/account/password", json={"current_password": "original-password", "new_password": "new-password-value"})
    assert response.status_code == 401
    assert store.one("SELECT password_hash FROM users WHERE id='owner'")["password_hash"] == old_hash


def test_oauth_audience_remains_bound_after_public_url_change(api):
    app, client, pat = api
    client_id, tokens = oauth_tokens(client)
    assert rpc(client, tokens["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200
    assert client.put("/api/settings", json={"public_url": "http://new-resource.test"}).status_code == 200
    assert rpc(client, tokens["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    response = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id, "resource": "http://new-resource.test/mcp"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert rpc(client, pat, {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200


def test_refresh_replay_revokes_family_but_wrong_client_does_not(api):
    app, client, _ = api
    client_id, old = oauth_tokens(client)
    form = {"grant_type": "refresh_token", "refresh_token": old["refresh_token"], "client_id": client_id}
    response = client.post("/oauth/token", data=form)
    assert response.status_code == 200
    new = response.json()
    assert client.post("/oauth/token", data={**form, "client_id": "unrelated-client"}).status_code == 400
    assert rpc(client, new["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200
    assert client.post("/oauth/token", data=form).status_code == 400
    assert rpc(client, new["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert client.post("/oauth/token", data={**form, "refresh_token": new["refresh_token"]}).status_code == 400


def test_health_checks_worker_and_database(api, monkeypatch):
    app, client, _ = api
    runtime, store = app.state.runtime, app.state.store
    assert client.get("/healthz").status_code == 200
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "worker", SimpleNamespace(done=lambda: True))
        assert client.get("/healthz").status_code == 503
    original = store.one
    def unavailable(sql, args=()):
        if sql == "SELECT 1 AS ok":
            raise sqlite3.OperationalError("private filesystem details")
        return original(sql, args)
    with monkeypatch.context() as patch:
        patch.setattr(store, "one", unavailable)
        response = client.get("/healthz")
        assert response.status_code == 503
        assert "private" not in response.text


@pytest.mark.parametrize("stage", ["startup", "shutdown"])
def test_lifespan_failure_closes_store_and_releases_instance_lock(tmp_path, monkeypatch, stage):
    app = create_app(str(tmp_path / "hub"))
    runtime = app.state.runtime
    original_stop = runtime.stop
    async def fail():
        if stage == "shutdown":
            await original_stop()
        raise RuntimeError("injected lifecycle failure")
    monkeypatch.setattr(runtime, "start" if stage == "startup" else "stop", fail)
    with pytest.raises(RuntimeError, match="injected lifecycle failure"):
        with TestClient(app):
            pass
    with pytest.raises(sqlite3.ProgrammingError):
        app.state.store.one("SELECT 1")
    lock = InstanceLock(tmp_path / "hub" / ".hub.lock")
    lock.close()


def test_initialization_failure_releases_instance_lock(tmp_path, monkeypatch):
    def fail(store):
        raise ValueError("invalid runtime configuration")
    with monkeypatch.context() as patch:
        patch.setattr(app_module, "Runtime", fail)
        with pytest.raises(ValueError, match="invalid runtime configuration"):
            create_app(str(tmp_path / "hub"))
    with TestClient(create_app(str(tmp_path / "hub"))) as client:
        assert client.get("/healthz").status_code == 200


def test_sse_watcher_cap_does_not_admit_extra_stream(api):
    app, client, _ = api
    app.state.runtime.watchers.update(asyncio.Queue() for _ in range(100))
    try:
        assert client.get("/api/events").status_code == 429
        assert len(app.state.runtime.watchers) == 100
    finally:
        app.state.runtime.watchers.clear()


def test_sse_start_send_failure_releases_watcher(api):
    app, _, _ = api
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/events")
    async def scenario():
        scope = {"type": "http", "method": "GET", "scheme": "http", "server": ("testserver", 80), "path": "/api/events", "headers": [(b"cookie", b"rd_session=session")], "asgi": {"spec_version": "2.4"}}
        response = await endpoint(Request(scope))
        assert len(app.state.runtime.watchers) == 1
        async def send(message):
            raise OSError("disconnected before headers")
        async def receive():
            return {"type": "http.disconnect"}
        from starlette.requests import ClientDisconnect
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
        assert not app.state.runtime.watchers
    asyncio.run(scenario())


def test_sse_session_revocation_while_waiting_drops_next_event(api):
    app, _, _ = api
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/events")
    async def scenario():
        scope = {"type": "http", "method": "GET", "scheme": "http", "server": ("testserver", 80), "path": "/api/events", "headers": [(b"cookie", b"rd_session=session")]}
        async def receive():
            await asyncio.Event().wait()
        response = await endpoint(Request(scope, receive))
        iterator = response.body_iterator
        assert "event: ready" in await anext(iterator)
        waiting = asyncio.create_task(anext(iterator))
        await asyncio.sleep(.01)
        app.state.store.execute("DELETE FROM sessions")
        app.state.runtime.publish("sensitive", {"value": "must not send"})
        with pytest.raises(StopAsyncIteration):
            await waiting
        assert not app.state.runtime.watchers
    asyncio.run(scenario())


def test_oauth_initialization_failure_releases_instance_lock(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv("MCP_PUBLIC_URL", "invalid://broken")
        with pytest.raises(ValueError):
            create_app(str(tmp_path / "hub"))
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://testserver")
    with TestClient(create_app(str(tmp_path / "hub"))) as client:
        assert client.get("/healthz").status_code == 200
