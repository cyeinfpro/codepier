"""Real in-process HTTP owner, CSRF, secrets and default-off boundaries."""

from dataclasses import replace
import time

from fastapi.testclient import TestClient
import pytest

from hub.app import create_app
from shared.crypto import digest, password_hash
from tests.legacy_iam_fixture import attach_session_security
from tests.collaboration_support import key

TOKEN = "900001:synthetic_telegram_token_fixture_only"


@pytest.fixture
def telegram_http(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    monkeypatch.setenv("CODEPIER_COLLABORATION_ENABLED", "true")
    monkeypatch.setenv("CODEPIER_MCP_EVENTS_ENABLED", "false")
    monkeypatch.setenv("CODEPIER_TELEGRAM_ENABLED", "false")
    app = create_app(str(tmp_path / "hub"))
    store = app.state.store
    store.execute(
        "INSERT INTO users VALUES (?,?,?,?)",
        ("owner", "admin", password_hash("fixture-password"), time.time()),
    )
    store.execute(
        "INSERT INTO sessions VALUES (?,?,?,?)",
        (digest("fixture-session"), "owner", "fixture-csrf", time.time() + 3600),
    )
    attach_session_security(store)
    store.execute(
        "INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)",
        ("fixture-device", "fixture", store.encrypt("x" * 43), time.time()),
    )
    store.execute(
        """INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created)
        VALUES ('fixture-project','fixture','fixture','fixture-device',?,'','write',1,?)""",
        (str(tmp_path), time.time()),
    )
    with TestClient(app) as client:
        client.cookies.set("rd_session", "fixture-session")
        client.headers["X-RD-CSRF"] = "fixture-csrf"
        scope = {"project": "fixture-project", "environment_id": "production"}
        response = client.post(
            "/api/collaboration/room", json={**scope, "idempotency_key": key()}
        )
        assert response.status_code == 200, response.text
        yield app, client, {**scope, "conversation_id": response.json()["room"]["id"]}


def request(scope):
    return {
        **scope,
        "bot_id": "900001",
        "token": TOKEN,
        "label": "Synthetic Telegram",
        "expected_version": 0,
        "confirm_credential_storage": True,
        "confirm_provider_terms": True,
        "idempotency_key": key(),
    }


def test_default_off_http_then_explicit_confirmed_account_registration(telegram_http):
    app, client, scope = telegram_http
    args = request(scope)
    assert (
        client.post("/api/collaboration/telegram-account", json=args).status_code == 409
    )
    service = app.state.runtime.collaboration
    service.config = replace(service.config, telegram_enabled=True)
    result = client.post("/api/collaboration/telegram-account", json=args)
    assert result.status_code == 200, result.text
    assert TOKEN not in result.text
    assert (
        client.post("/api/collaboration/telegram-account", json=args).json()
        == result.json()
    )
    listing = client.get("/api/collaboration/telegram", params=scope)
    assert listing.status_code == 200 and listing.json()["mode"] == "long_poll"
    assert TOKEN not in listing.text and "secret" not in listing.text
    assert listing.json()["accounts"][0]["bot_id"] == "900001"
    stored = app.state.store.one("SELECT secret FROM telegram_accounts")["secret"]
    assert TOKEN not in stored


def test_csrf_auth_and_consent_cannot_be_skipped(telegram_http):
    app, client, scope = telegram_http
    service = app.state.runtime.collaboration
    service.config = replace(service.config, telegram_enabled=True)
    args = request(scope)
    assert (
        client.post(
            "/api/collaboration/telegram-account",
            json=args,
            headers={"X-RD-CSRF": "wrong"},
        ).status_code
        == 403
    )
    args["confirm_credential_storage"] = False
    assert (
        client.post("/api/collaboration/telegram-account", json=args).status_code == 422
    )
    assert app.state.store.all("SELECT * FROM telegram_accounts") == []
    client.cookies.clear()
    assert client.get("/api/collaboration/telegram", params=scope).status_code in (
        401,
        403,
    )
    assert client.post(
        "/api/collaboration/telegram-account", json=request(scope)
    ).status_code in (401, 403)
