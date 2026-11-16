"""Gemini submits all six Connected Apps callbacks in one DCR request."""

from itertools import product

from tests.test_audit_api import api as api


def test_gemini_web_can_register_all_callbacks(monkeypatch, request):
    monkeypatch.delenv("OAUTH_REDIRECT_HOSTS", raising=False)
    monkeypatch.delenv("MCP_OAUTH_REDIRECT_HOSTS", raising=False)
    _, client, _ = request.getfixturevalue("api")
    hosts = (
        "oauth-redirect-sandbox.googleusercontent.com",
        "oauth-redirect-test.googleusercontent.com",
        "oauth-redirect.googleusercontent.com",
    )
    redirects = [
        f"https://{host}/{kind}/user_bound_custom-mcp-example"
        for kind, host in product(("r", "a"), hosts)
    ]
    body = {
        "client_name": "Gemini",
        "redirect_uris": redirects,
        "token_endpoint_auth_method": "none",
    }
    registered = client.post("/oauth/register", json=body)
    assert registered.status_code == 201, registered.text
    assert registered.json()["redirect_uris"] == redirects
    assert registered.json()["token_endpoint_auth_method"] == "none"

    untrusted = "https://oauth-redirect.googleusercontent.com.example.test/r/example"
    refused = client.post("/oauth/register", json={**body, "redirect_uris": [*redirects, untrusted]})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "INVALID_REDIRECT"
