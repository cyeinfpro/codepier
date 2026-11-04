"""Synthetic connection lifetime regression, no external credentials."""
import time
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit
import pytest
from fastapi import Request
from hub.principal import validate_credential
from shared.crypto import digest
from shared.util import DevError
from tests.test_audit_api import api as api, oauth_tokens, rpc


def create(client, **extra):
    response = client.post("/api/grants", json={"label": "expiry test", "scopes": ["read"], "projects": ["project"], **extra})
    assert response.status_code == 200, response.text
    return response.json()


def edit(client, gid, expires, revision=1, confirm=False):
    return client.put(f"/api/grants/{gid}/expiration", json={
        "expires": expires, "expected_revision": revision, "confirm_reactivate": confirm})


@pytest.mark.parametrize("days", [None, 7, "bad", True, 0])
def test_oauth_explicit_lifetime(api, days):
    app, client, _ = api
    callback = "http://localhost:12345/callback"
    registration = client.post("/oauth/register", json={"redirect_uris": [callback]}).json()
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    response = client.get("/oauth/authorize", params={"response_type": "code",
        "client_id": registration["client_id"], "redirect_uri": callback,
        "code_challenge_method": "S256", "code_challenge": challenge,
        "resource": "http://testserver/mcp"}, follow_redirects=False)
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    decision = client.post(f"/api/oauth/requests/{request_id}/decide", json={
        "allow": True, "scopes": ["read"], "projects": ["project"], "days": days})
    if days not in (None, 7) or type(days) is bool:
        assert decision.status_code == 400
        return
    assert decision.status_code == 200, decision.text
    code = parse_qs(urlsplit(decision.json()["redirect"]).query)["code"][0]
    tokens = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
        "client_id": registration["client_id"], "redirect_uri": callback, "code_verifier": verifier})
    assert tokens.status_code == 200, tokens.text
    token_row = app.state.store.one("SELECT * FROM tokens WHERE hash=?", (digest(tokens.json()["refresh_token"]),))
    lifetime = app.state.store.one("SELECT * FROM grant_expirations WHERE grant_id=?", (token_row["grant_id"],))
    if days is None:
        assert lifetime["expires"] is None
        assert token_row["expires"] > time.time() + 365 * 86400
    else:
        assert 6.99 * 86400 < lifetime["expires"] - time.time() <= 7 * 86400
        assert token_row["expires"] == lifetime["expires"]
    assert tokens.json()["expires_in"] == 3600


def test_edit_expiration_csrf_origin_and_space(api):
    app, client, _ = api
    item = create(client)
    gid = item["grant_id"]
    client.headers["X-RD-CSRF"] = "wrong"
    assert edit(client, gid, None).status_code == 403
    client.headers["X-RD-CSRF"] = "csrf"
    client.headers["Origin"] = "https://evil.example"
    assert edit(client, gid, None).status_code == 403
    del client.headers["Origin"]
    # An unrelated Space exists, but this session has no ownership there.
    app.state.store.execute("INSERT INTO spaces(id,label,kind,created) VALUES (?,?,?,?)", ("other-space", "other", "team", time.time()))
    foreign = app.state.store.one("SELECT * FROM grants WHERE id=?", (gid,))
    foreign.update(id="foreign-grant", space_id="other-space")
    columns = ",".join(foreign)
    placeholders = ",".join("?" for _ in foreign)
    app.state.store.execute(f"INSERT INTO grants({columns}) VALUES ({placeholders})", tuple(foreign.values()))
    assert edit(client, "foreign-grant", None).status_code == 404


def test_expired_grant_blocks_queued_dispatch_and_internal_principal(api):
    import asyncio
    from hub import iam
    from hub.access_profiles import effective_grant
    from hub.operation_history import result as operation_result

    app, client, _ = api
    store, runtime = app.state.store, app.state.runtime
    credential = create(client, scopes=["read", "write"])
    grant = store.one("SELECT * FROM grants WHERE id=?", (credential["grant_id"],))
    principal = runtime.grant_principal(grant)
    assert not principal.token_hash
    runtime.wait_seconds = 0

    async def scenario():
        queued = await runtime.invoke("fs_write", {"project": "fixture", "path": "never-written.txt",
            "content": "MUST NOT EXECUTE", "expected_sha256": "new", "idempotency_key": "queued-expiry-test"}, principal)
        identifier = queued["operation_id"]
        before = store.one("SELECT * FROM operations WHERE id=?", (identifier,))
        assert before["attempts"] == 0 and not before["accepted_at"]
        store.execute("UPDATE grant_expirations SET expires=? WHERE grant_id=?", (time.time() - 1, grant["id"]))
        for check in (lambda: effective_grant(store, grant), lambda: iam.validate_grant(store, grant),
                      lambda: runtime.grant_principal(grant)):
            with pytest.raises(DevError, match="过期"):
                check()
        await runtime.deliver(identifier)
        after = store.one("SELECT * FROM operations WHERE id=?", (identifier,))
        assert after["attempts"] == 0 and not after["accepted_at"]
        assert operation_result(store, after)["error"]["code"] == "AUTHORIZATION_CHANGED"

    async def bounded():
        await asyncio.wait_for(scenario(), timeout=10)
    client.portal.call(bounded)


def test_default_unlimited_and_revocation(api):
    app, client, _ = api
    finite = create(client)
    assert 29.9 * 86400 < finite["expires"] - time.time() <= 30 * 86400
    unlimited = create(client, days=None)
    assert unlimited["expires"] is None and unlimited["unlimited"]
    row = next(g for g in client.get("/api/grants").json()["grants"] if g["id"] == unlimited["grant_id"])
    assert row["status"] == "active" and row["unlimited"] and row["expires"] is None
    assert rpc(client, unlimited["token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200
    assert client.delete("/api/grants/" + unlimited["grant_id"]).status_code == 200
    assert edit(client, unlimited["grant_id"], None, confirm=True).status_code == 409
    assert rpc(client, unlimited["token"], {"jsonrpc": "2.0", "id": 2, "method": "ping"}).status_code == 401


@pytest.mark.parametrize("days", [0, -1, 366, True, "30", 1.5])
def test_invalid_days(api, days):
    _, client, _ = api
    assert client.post("/api/grants", json={"label": "bad", "scopes": ["read"], "projects": ["project"], "days": days}).status_code == 422


def test_edit_expiry_cas_shorten_and_legacy(api):
    app, client, _ = api
    item = create(client, days=None)
    gid = item["grant_id"]
    request = Request({"type": "http", "headers": [(b"authorization", ("Bearer " + item["token"]).encode())]})
    principal = app.state.auth.bearer(request)
    end = time.time() + 100
    assert edit(client, gid, end).status_code == 200
    assert edit(client, gid, None).status_code == 409
    assert app.state.store.one("SELECT expires FROM tokens WHERE grant_id=?", (gid,))["expires"] == end
    app.state.store.execute("UPDATE grant_expirations SET expires=? WHERE grant_id=?", (time.time() - 1, gid))
    with pytest.raises(DevError):
        validate_credential(app.state.store, principal)
    assert edit(client, gid, None, revision=2).status_code == 409
    assert edit(client, gid, None, revision=2, confirm=True).status_code == 200
    assert app.state.auth.bearer(request).grant_id == gid
    app.state.store.execute("DELETE FROM grant_expirations WHERE grant_id=?", (gid,))
    assert app.state.auth.bearer(request).grant_id == gid
    assert edit(client, gid, time.time() + 200, revision=0).status_code == 200
    assert client.put(f"/api/grants/{gid}/expiration", json={"expected_revision": 1}).status_code == 422


def test_deleted_credentials_not_recreated_and_owner_boundary(api):
    app, client, _ = api
    item = create(client)
    gid = item["grant_id"]
    app.state.store.execute("DELETE FROM tokens WHERE grant_id=?", (gid,))
    response = edit(client, gid, None, confirm=True)
    assert response.status_code == 200 and response.json()["reconnect_required"]
    assert app.state.store.one("SELECT count(*) AS n FROM tokens WHERE grant_id=?", (gid,))["n"] == 0
    app.state.store.execute("INSERT INTO users VALUES (?,?,?,?)", ("other", "other", "unused-synthetic", time.time()))
    app.state.store.execute("UPDATE grants SET user_id='other' WHERE id=?", (gid,))
    assert edit(client, gid, None, revision=2, confirm=True).status_code == 404


def test_oauth_rotation_lifetime_and_consumed_token_not_extended(api):
    app, client, _ = api
    client_id, tokens = oauth_tokens(client)
    store = app.state.store
    row = store.one("SELECT * FROM tokens WHERE hash=?", (digest(tokens["refresh_token"]),))
    gid = row["grant_id"]
    assert edit(client, gid, None).status_code == 200
    form = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]}
    rotated = client.post("/oauth/token", data=form)
    assert rotated.status_code == 200, rotated.text
    fresh = rotated.json()
    assert fresh["expires_in"] == 3600
    consumed = store.one("SELECT * FROM tokens WHERE hash=?", (digest(tokens["refresh_token"]),))
    end = time.time() + 60
    assert edit(client, gid, end, revision=2).status_code == 200
    assert store.one("SELECT expires FROM tokens WHERE hash=?", (digest(fresh["refresh_token"]),))["expires"] == end
    assert store.one("SELECT expires FROM tokens WHERE hash=?", (digest(fresh["access_token"]),))["expires"] == end
    assert store.one("SELECT expires FROM tokens WHERE hash=?", (digest(tokens["refresh_token"]),))["expires"] == consumed["expires"]
    assert client.post("/oauth/token", data=form).status_code == 400
    assert edit(client, gid, None, revision=3, confirm=True).status_code == 409
