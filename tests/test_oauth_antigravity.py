"""Antigravity DCR compatibility and redirect boundaries (no external account)."""
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.test_audit_api import api as api


CALLBACK = "https://antigravity.google/oauth-callback"


@pytest.fixture
def antigravity_api(monkeypatch, request):
    monkeypatch.delenv("OAUTH_REDIRECT_HOSTS", raising=False)
    monkeypatch.delenv("MCP_OAUTH_REDIRECT_HOSTS", raising=False)
    return request.getfixturevalue("api")


def register(client, callback=CALLBACK):
    return client.post("/oauth/register", json={
        "client_name": "Antigravity", "redirect_uris": [callback],
        "token_endpoint_auth_method": "none",
    })


def test_dcr_consent_pkce_token_and_tool_discovery(antigravity_api):
    app, client, _ = antigravity_api
    registered = register(client)
    assert registered.status_code == 201, registered.text
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": registered.json()["client_id"],
              "redirect_uri": CALLBACK, "code_challenge_method": "S256",
              "code_challenge": challenge, "scope": "read",
              "resource": "http://testserver/mcp", "state": "test-state"}
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 303, response.text
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    decision = client.post(f"/api/oauth/requests/{request_id}/decide", json={
        "allow": True, "scopes": ["read"], "projects": ["project"]})
    assert decision.status_code == 200, decision.text
    redirect = urlsplit(decision.json()["redirect"])
    assert redirect.scheme == "https" and redirect.netloc == "antigravity.google"
    assert redirect.path == "/oauth-callback"
    query = parse_qs(redirect.query)
    assert query["state"] == ["test-state"]
    form = {"grant_type": "authorization_code", "code": query["code"][0],
            "client_id": registered.json()["client_id"], "redirect_uri": CALLBACK,
            "code_verifier": verifier}
    assert client.post("/oauth/token", data={**form, "code_verifier": "wrong" * 16}).status_code == 400
    tokens = client.post("/oauth/token", data=form)
    assert tokens.status_code == 200, tokens.text
    headers = {"Authorization": "Bearer " + tokens.json()["access_token"],
               "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list", "params": {}}, headers=headers)
    assert tools.status_code == 200, tools.text
    assert tools.json()["result"]["tools"]


@pytest.mark.parametrize("callback", [
    "http://antigravity.google/oauth-callback",
    "https://antigravity.google.attacker.example/oauth-callback",
    "https://evil.antigravity.google/oauth-callback",
    "https://antigravity.google@attacker.example/oauth-callback",
    CALLBACK + "#fragment",
])
def test_redirect_security_boundaries(antigravity_api, callback):
    _, client, _ = antigravity_api
    assert register(client, callback).status_code == 400


def test_custom_policy_is_preserved(monkeypatch, request):
    monkeypatch.setenv("OAUTH_REDIRECT_HOSTS", "chatgpt.com,localhost")
    monkeypatch.delenv("MCP_OAUTH_REDIRECT_HOSTS", raising=False)
    _, client, _ = request.getfixturevalue("api")
    assert register(client).status_code == 400
