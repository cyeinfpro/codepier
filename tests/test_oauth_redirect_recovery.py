"""Bounded on-origin recovery for one observed Antigravity link corruption."""
import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from tests.test_audit_api import api as api
from tests.test_oauth_antigravity import antigravity_api as antigravity_api, register, CALLBACK

BROKEN = "h ttps://antigravity.google/oauth-callback"


def request_params(client, **changes):
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return {"response_type": "code", "client_id": register(client).json()["client_id"],
            "redirect_uri": BROKEN, "code_challenge_method": "S256",
            "code_challenge": challenge, "scope": "read", "resource": "http://testserver/mcp",
            "state": "s+&=%20/#中文", **changes}


def counts(app):
    return {table: app.state.store.one(f"SELECT count(*) AS n FROM {table}")["n"]
            for table in ("oauth_requests", "oauth_codes", "grants", "tokens")}


def test_recovery_full_consent_exchange_and_replay_boundaries(antigravity_api):
    app, client, _ = antigravity_api
    params = request_params(client)
    before = counts(app)
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 303
    assert counts(app) == before
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    target = urlsplit(response.headers["location"])
    assert not target.scheme and not target.netloc
    assert target.path == "/oauth/authorize"
    assert parse_qs(target.query) == {k: [v] for k, v in {**params, "redirect_uri": CALLBACK}.items()}
    # Retrying the damaged request creates no authorization record either.
    client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert counts(app) == before
    response = client.get(response.headers["location"], follow_redirects=False)
    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/"
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    row = app.state.store.one("SELECT * FROM oauth_requests WHERE id=?", (request_id,))
    assert row["redirect_uri"] == CALLBACK
    assert row["state"] == params["state"]
    assert row["challenge"] == params["code_challenge"]
    assert row["resource"] == params["resource"]
    assert counts(app)["grants"] == before["grants"]
    decision = client.post(f"/api/oauth/requests/{request_id}/decide",
                          json={"allow": True, "scopes": ["read"], "projects": ["project"]})
    assert decision.status_code == 200, decision.text
    callback = urlsplit(decision.json()["redirect"])
    assert callback.scheme + "://" + callback.netloc + callback.path == CALLBACK
    query = parse_qs(callback.query)
    assert query["state"] == [params["state"]]
    code = query["code"][0]
    token = {"grant_type": "authorization_code", "client_id": params["client_id"],
             "code": code, "code_verifier": "v" * 64, "redirect_uri": CALLBACK}
    assert client.post("/oauth/token", data={**token, "redirect_uri": BROKEN}).status_code == 400
    assert client.post("/oauth/token", data={**token, "redirect_uri": CALLBACK + "/"}).status_code == 400
    assert client.post("/oauth/token", data={**token, "code_verifier": "x" * 64}).status_code == 400
    exchanged = client.post("/oauth/token", data=token)
    assert exchanged.status_code == 200, exchanged.text
    assert client.post("/oauth/token", data=token).status_code == 400
    assert client.post(f"/api/oauth/requests/{request_id}/decide",
                       json={"allow": True, "scopes": ["read"], "projects": ["project"]}).status_code >= 400
    rpc = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                      headers={"Authorization": "Bearer " + exchanged.json()["access_token"],
                               "Accept": "application/json, text/event-stream"})
    assert rpc.status_code == 200


@pytest.mark.parametrize("bad", [
    " h ttps://antigravity.google/oauth-callback",
    "h  ttps://antigravity.google/oauth-callback",
    "h\tttps://antigravity.google/oauth-callback",
    "h\u00a0ttps://antigravity.google/oauth-callback",
    BROKEN + "/", BROKEN + "?next=https://evil.example", BROKEN + "#x",
    BROKEN.replace("antigravity.google", "antigravity.google.evil.example"),
    BROKEN.replace("antigravity.google", "antigravity.google@evil.example"),
    BROKEN.replace("antigravity.google", "ANTIGRAVITY.GOOGLE"),
    BROKEN.replace("h ttps", "h%20ttps"),
    BROKEN.replace("/oauth-callback", "/%6fauth-callback"),
    "//evil.example", "https://evil.example",
])
def test_no_generic_normalization_or_open_redirect(antigravity_api, bad):
    app, client, _ = antigravity_api
    params = request_params(client, redirect_uri=bad)
    before = counts(app)
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 400
    assert "location" not in response.headers
    assert counts(app) == before


@pytest.mark.parametrize("changes", [
    {"client_id": "unknown"}, {"code_challenge": ""},
    {"code_challenge_method": "plain"}, {"response_type": "token"},
    {"resource": "https://evil.example/mcp"}, {"scope": "admin"},
    {"scope": ""}, {"state": "x" * 2049}, {"prompt": "none"},
    {"request_uri": "https://evil.example"}, {"redirect": "https://evil.example"},
])
def test_complete_validation_precedes_recovery(antigravity_api, changes):
    app, client, _ = antigravity_api
    params = request_params(client, **changes)
    before = counts(app)
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 400
    assert "location" not in response.headers
    assert counts(app) == before


@pytest.mark.parametrize("key", ["client_id", "redirect_uri", "state", "resource", "code_challenge", "scope"])
def test_duplicate_parameters_rejected_before_recovery(antigravity_api, key):
    app, client, _ = antigravity_api
    params = request_params(client)
    before = counts(app)
    response = client.get("/oauth/authorize?" + urlencode(params) + "&" + urlencode({key: params[key]}),
                          follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert counts(app) == before


@pytest.mark.parametrize("redirects", [
    [CALLBACK, CALLBACK], [CALLBACK, "https://antigravity.google/other"],
    ["https://antigravity.google/other"], ["https://chatgpt.com/callback"],
])
def test_registration_must_be_unique_exact_target(antigravity_api, redirects):
    app, client, _ = antigravity_api
    params = request_params(client)
    params["client_id"] = client.post("/oauth/register", json={"redirect_uris": redirects}).json()["client_id"]
    before = counts(app)
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 400
    assert counts(app) == before


@pytest.mark.parametrize("state", [None, "", "a+b&c=d%20#fragment", "<script>坏</script>"])
def test_absent_empty_and_encoded_state_preserved(antigravity_api, state):
    _, client, _ = antigravity_api
    params = request_params(client)
    del params["scope"]
    del params["resource"]
    if state is None:
        del params["state"]
    else:
        params["state"] = state
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 303
    corrected = parse_qs(urlsplit(response.headers["location"]).query, keep_blank_values=True)
    assert corrected == {k: [v] for k, v in {**params, "redirect_uri": CALLBACK}.items()}
