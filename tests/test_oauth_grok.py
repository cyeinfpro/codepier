"""Grok hosted OAuth compatibility without real credentials or network calls."""
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_audit_api import api as api
from scripts.panel_update_runtime import OAUTH_CURRENT_DEFAULT, OAUTH_LEGACY_DEFAULTS


CALLBACK = "https://grok.com/connectors-oauth-exchange-code/"


@pytest.fixture
def grok_api(monkeypatch, request):
    monkeypatch.delenv("OAUTH_REDIRECT_HOSTS", raising=False)
    monkeypatch.delenv("MCP_OAUTH_REDIRECT_HOSTS", raising=False)
    return request.getfixturevalue("api")


def register(client, callback=CALLBACK):
    return client.post("/oauth/register", json={
        "client_name": "Grok", "redirect_uris": [callback],
        "token_endpoint_auth_method": "none",
    })


def test_grok_dcr_and_pkce_wait_for_explicit_consent(grok_api):
    app, client, _ = grok_api
    before = {table: app.state.store.one(f"SELECT count(*) AS n FROM {table}")["n"]
              for table in ("grants", "tokens", "oauth_codes")}
    registered = register(client)
    assert registered.status_code == 201, registered.text
    body = registered.json()
    assert body["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in body
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": body["client_id"],
        "redirect_uri": CALLBACK, "code_challenge_method": "S256",
        "code_challenge": challenge, "scope": "read",
        "resource": "http://testserver/mcp",
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    row = app.state.store.one("SELECT * FROM oauth_requests WHERE id=?", (request_id,))
    assert row["redirect_uri"] == CALLBACK
    assert json.loads(row["scopes"]) == ["read"]
    for table, count in before.items():
        assert app.state.store.one(f"SELECT count(*) AS n FROM {table}")["n"] == count


@pytest.mark.parametrize("callback", [
    "http://grok.com/connectors-oauth-exchange-code/",
    "https://grok.com.attacker.example/connectors-oauth-exchange-code/",
    "https://evil.grok.com/connectors-oauth-exchange-code/",
    "https://grok.com@attacker.example/connectors-oauth-exchange-code/",
    CALLBACK + "#fragment",
])
def test_grok_redirect_security_boundaries(grok_api, callback):
    app, client, _ = grok_api
    before = app.state.store.one("SELECT count(*) AS n FROM oauth_clients")["n"]
    response = register(client, callback)
    assert response.status_code == 400, response.text
    assert app.state.store.one("SELECT count(*) AS n FROM oauth_clients")["n"] == before


def test_explicit_operator_allowlist_is_not_expanded(monkeypatch, request):
    monkeypatch.delenv("OAUTH_REDIRECT_HOSTS", raising=False)
    monkeypatch.delenv("MCP_OAUTH_REDIRECT_HOSTS", raising=False)
    monkeypatch.setenv("OAUTH_REDIRECT_HOSTS", "chatgpt.com,localhost")
    _, client, _ = request.getfixturevalue("api")
    response = register(client)
    assert response.status_code == 400, response.text


def test_deployment_default_allowlists_include_grok():
    root = Path(__file__).resolve().parents[1]
    for filename in (".env.example", "compose.yml", "deploy/k3s/deployment.yaml"):
        line = next(line for line in (root / filename).read_text().splitlines()
                    if "OAUTH_REDIRECT_HOSTS" in line)
        assert OAUTH_CURRENT_DEFAULT in line, filename


def test_grok_consent_token_and_mcp_tool_discovery(grok_api):
    app, client, _ = grok_api
    registered = register(client).json()
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "response_type": "code", "client_id": registered["client_id"],
        "redirect_uri": CALLBACK, "code_challenge_method": "S256",
        "code_challenge": challenge, "scope": "read", "state": "grok-test-state",
        "resource": "http://testserver/mcp",
    }
    for invalid in ({"redirect_uri": CALLBACK.rstrip("/")}, {"code_challenge_method": "plain"}):
        assert client.get("/oauth/authorize", params={**params, **invalid}, follow_redirects=False).status_code == 400
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 303, response.text
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    decision = client.post(f"/api/oauth/requests/{request_id}/decide", json={
        "allow": True, "scopes": ["read"], "projects": ["project"],
    })
    assert decision.status_code == 200, decision.text
    redirect = urlsplit(decision.json()["redirect"])
    assert redirect.scheme == "https" and redirect.netloc == "grok.com"
    assert redirect.path == urlsplit(CALLBACK).path
    query = parse_qs(redirect.query)
    assert query["state"] == ["grok-test-state"]
    form = {"grant_type": "authorization_code", "code": query["code"][0],
            "client_id": registered["client_id"], "redirect_uri": CALLBACK, "code_verifier": verifier}
    assert client.post("/oauth/token", data={**form, "code_verifier": "wrong" * 16}).status_code == 400
    tokens = client.post("/oauth/token", data=form)
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["scope"] == "read"
    headers = {"Authorization": "Bearer " + tokens.json()["access_token"],
               "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    before = app.state.store.one("SELECT count(*) AS n FROM operations")["n"]
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=headers)
    assert tools.status_code == 200, tools.text
    assert tools.json()["result"]["tools"]
    assert app.state.store.one("SELECT count(*) AS n FROM operations")["n"] == before


@pytest.mark.parametrize("legacy", OAUTH_LEGACY_DEFAULTS)
@pytest.mark.parametrize("quote", ["", "'", '"'])
@pytest.mark.parametrize("newline", ["", "\n", "\r\n"])
def test_installer_migrates_shipped_oauth_defaults(tmp_path, legacy, quote, newline):
    before = f"# preserved\nOAUTH_REDIRECT_HOSTS={quote}{legacy}{quote}{newline}".encode()
    expected = before.replace(legacy.encode(), OAUTH_CURRENT_DEFAULT.encode())
    run_installer_migration(tmp_path, before, expected)
    run_installer_migration(tmp_path, expected, expected)


@pytest.mark.parametrize("value", ["custom.example", OAUTH_CURRENT_DEFAULT,
                                    *[legacy + ",custom.example" for legacy in OAUTH_LEGACY_DEFAULTS]])
def test_installer_preserves_explicit_custom_policy(tmp_path, value):
    before = f"OAUTH_REDIRECT_HOSTS='{value}'\r\nOTHER=unchanged\r\n".encode()
    run_installer_migration(tmp_path, before, before)


def run_installer_migration(tmp_path, before, expected):
    script = (Path(__file__).resolve().parents[1] / "deploy/install-hub.sh").read_text()
    code = script.split("<<'PYOAUTH'\n", 1)[1].split("\nPYOAUTH", 1)[0]
    env = tmp_path / ".env"
    env.write_bytes(before)
    env.chmod(0o600)
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, capture_output=True, timeout=10)
    assert env.read_bytes() == expected
    assert env.stat().st_mode & 0o777 == 0o600
