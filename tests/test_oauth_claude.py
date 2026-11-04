"""Claude hosted OAuth compatibility without real credentials or network calls."""
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_audit_api import api as api


CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def claude_api(monkeypatch, request):
    monkeypatch.delenv("OAUTH_REDIRECT_HOSTS", raising=False)
    monkeypatch.delenv("MCP_OAUTH_REDIRECT_HOSTS", raising=False)
    return request.getfixturevalue("api")


def register(client, callback=CALLBACK):
    return client.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [callback],
        "token_endpoint_auth_method": "none",
    })


def test_claude_dcr_and_pkce_wait_for_explicit_consent(claude_api):
    app, client, _ = claude_api
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
    "http://claude.ai/api/mcp/auth_callback",
    "https://claude.ai.attacker.example/api/mcp/auth_callback",
    "https://evil.claude.ai/api/mcp/auth_callback",
    "https://claude.ai@attacker.example/api/mcp/auth_callback",
    CALLBACK + "#fragment",
])
def test_claude_redirect_security_boundaries(claude_api, callback):
    app, client, _ = claude_api
    before = app.state.store.one("SELECT count(*) AS n FROM oauth_clients")["n"]
    response = register(client, callback)
    assert response.status_code == 400, response.text
    assert app.state.store.one("SELECT count(*) AS n FROM oauth_clients")["n"] == before


def test_explicit_operator_allowlist_is_not_expanded(monkeypatch, request):
    monkeypatch.setenv("OAUTH_REDIRECT_HOSTS", "chatgpt.com,localhost")
    _, client, _ = request.getfixturevalue("api")
    response = register(client)
    assert response.status_code == 400, response.text


def test_deployment_default_allowlists_include_claude():
    root = Path(__file__).resolve().parents[1]
    for filename in (".env.example", "compose.yml", "deploy/k3s/deployment.yaml"):
        line = next(line for line in (root / filename).read_text().splitlines()
                    if "OAUTH_REDIRECT_HOSTS" in line)
        assert "claude.ai" in line, filename
