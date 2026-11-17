"""Upgrade existing logged-in clients using synthetic credentials only."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Request

from shared.crypto import digest
from shared.util import DevError
from tests.test_audit_api import api as api, oauth_tokens, rpc
from tests.oauth_legacy import legacy_issuer, migrate

PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def grant(app, value):
    return app.state.store.one(
        "SELECT g.* FROM grants g JOIN tokens t ON t.grant_id=g.id WHERE t.hash=?",
        (digest(value),))


def refresh(client, cid, value, **extra):
    return client.post("/oauth/token", data={
        "grant_type": "refresh_token", "client_id": cid, "refresh_token": value, **extra})


def old_connection(app, client, monkeypatch, unlimited=False):
    with legacy_issuer(monkeypatch):
        cid, old = oauth_tokens(client)
    gid = grant(app, old["access_token"])["id"]
    if unlimited:
        response = client.put("/api/grants/" + gid + "/expiration",
                              json={"expires": None, "expected_revision": 1})
        assert response.status_code == 200
    return cid, old, gid


def test_new_oauth_uses_authorized_lifetime_without_refresh(api):
    app, client, _ = api
    _, result = oauth_tokens(client)
    assert "refresh_token" not in result
    assert result["expires_in"] > 29 * 86400
    row = app.state.store.one("SELECT * FROM tokens WHERE hash=?",
                              (digest(result["access_token"]),))
    assert row["kind"] == "access"
    assert rpc(client, result["access_token"], PING).status_code == 200


def test_existing_clients_migrate_without_login_or_token_replacement(api, monkeypatch):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch, unlimited=True)
    before = grant(app, old["access_token"])
    migrate(app)
    assert grant(app, old["access_token"]) == before
    assert rpc(client, old["access_token"], PING).status_code == 200
    first = refresh(client, cid, old["refresh_token"])
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["refresh_token"] == old["refresh_token"]
    assert "expires_in" not in result
    # Retry after a lost response, then wake a stale process much later.
    assert refresh(client, cid, old["refresh_token"]).json() == result
    real_time = time.time
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: real_time() + 3 * 86400)
        assert refresh(client, cid, old["refresh_token"]).json() == result
        assert rpc(client, old["access_token"], PING).status_code == 200
    migrate(app)
    assert refresh(client, cid, old["refresh_token"]).json() == result
    assert grant(app, result["access_token"])["id"] == gid
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["pragma"] == "no-cache"
    assert not app.state.store.one("SELECT revoked FROM grants WHERE id=?", (gid,))["revoked"]
    # No credentials in audit; stable response at rest is encrypted.
    audit = json.dumps(app.state.store.all("SELECT detail FROM audit"))
    assert old["refresh_token"] not in audit and result["access_token"] not in audit
    saved = app.state.store.one("SELECT * FROM oauth_lifetime_credentials WHERE grant_id=?", (gid,))
    assert result["access_token"] not in json.dumps(saved)


def test_concurrent_refresh_converges_on_one_credential(api, monkeypatch):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch, unlimited=True)
    migrate(app)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: refresh(client, cid, old["refresh_token"]), range(6)))
    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["access_token"] for r in responses}) == 1
    assert {r.json()["refresh_token"] for r in responses} == {old["refresh_token"]}
    assert app.state.store.one(
        "SELECT count(*) n FROM oauth_lifetime_credentials WHERE grant_id=?", (gid,))["n"] == 1


def test_invalid_client_resource_scope_cannot_mutate_migration(api, monkeypatch):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch)
    migrate(app)
    assert refresh(client, "wrong", old["refresh_token"]).status_code == 400
    assert refresh(client, cid, old["refresh_token"], resource="https://wrong/mcp").status_code == 400
    assert refresh(client, cid, old["refresh_token"], scope="read write").json()["error"] == "invalid_scope"
    assert not app.state.store.one("SELECT * FROM oauth_lifetime_credentials WHERE grant_id=?", (gid,))
    assert refresh(client, cid, old["refresh_token"]).status_code == 200


@pytest.mark.parametrize("state", ["revoked", "expired", "deleted", "consumed"])
def test_upgrade_never_revives_invalid_credentials(api, monkeypatch, state):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch)
    store = app.state.store
    if state == "revoked":
        store.execute("UPDATE grants SET revoked=1 WHERE id=?", (gid,))
    elif state == "expired":
        store.execute("UPDATE tokens SET expires=? WHERE grant_id=?", (time.time() - 1, gid))
    elif state == "deleted":
        store.execute("DELETE FROM tokens WHERE grant_id=?", (gid,))
    else:
        store.execute("UPDATE tokens SET kind='refresh_used' WHERE hash=?", (digest(old["refresh_token"]),))
    migrate(app)
    assert refresh(client, cid, old["refresh_token"]).status_code == 400
    assert not store.one("SELECT * FROM oauth_refresh_migrations WHERE token_hash=?",
                         (digest(old["refresh_token"]),))


def test_consumed_old_token_cannot_revoke_migrated_connection(api, monkeypatch):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch)
    store = app.state.store
    with store.transaction():
        store.db.execute("UPDATE tokens SET kind='refresh_used' WHERE hash=?", (digest(old["refresh_token"]),))
        with legacy_issuer(monkeypatch):
            from hub.oauth import OAuth
            fresh = OAuth.new_tokens(app.state.runtime.oauth, gid)
    migrate(app)
    assert refresh(client, cid, fresh["refresh_token"]).status_code == 200
    assert refresh(client, cid, old["refresh_token"]).status_code == 400
    assert rpc(client, fresh["access_token"], PING).status_code == 200
    assert not store.one("SELECT revoked FROM grants WHERE id=?", (gid,))["revoked"]


def test_revoke_and_expiration_apply_to_all_old_and_new_credentials(api, monkeypatch):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch, unlimited=True)
    migrate(app)
    new = refresh(client, cid, old["refresh_token"]).json()
    end = time.time() + 120
    changed = client.put("/api/grants/" + gid + "/expiration",
                         json={"expires": end, "expected_revision": 2})
    assert changed.status_code == 200, changed.text
    assert 0 < refresh(client, cid, old["refresh_token"]).json()["expires_in"] <= 120
    assert client.delete("/api/grants/" + gid).status_code == 200
    assert rpc(client, old["access_token"], PING).status_code == 401
    assert rpc(client, new["access_token"], PING).status_code == 401
    assert refresh(client, cid, old["refresh_token"]).status_code == 400


def test_missing_legacy_policy_preserves_original_absolute_deadline(api, monkeypatch):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch)
    store = app.state.store
    deadline = time.time() + 5000
    store.execute("DELETE FROM grant_expirations WHERE grant_id=?", (gid,))
    store.execute("UPDATE tokens SET expires=? WHERE hash=?", (deadline, digest(old["refresh_token"])))
    migrate(app)
    assert store.one("SELECT expires FROM grant_expirations WHERE grant_id=?", (gid,))["expires"] == deadline
    new = refresh(client, cid, old["refresh_token"]).json()
    assert 4900 < new["expires_in"] <= 5000


def test_deleted_migration_result_is_not_recreated(api, monkeypatch):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch)
    migrate(app)
    result = refresh(client, cid, old["refresh_token"]).json()
    app.state.store.execute("DELETE FROM tokens WHERE hash=?", (digest(result["access_token"]),))
    assert refresh(client, cid, old["refresh_token"]).status_code == 400


def test_older_writer_cannot_open_migrated_database(api):
    from shared.hub_data_contract import check_database
    app, _, _ = api
    with app.state.store.lock:
        with pytest.raises(RuntimeError, match="cannot open"):
            check_database(app.state.store.db, "1.33.22")


@pytest.mark.parametrize("rekey", [False, True])
def test_restart_and_offline_rekey_preserve_existing_client(tmp_path, monkeypatch, rekey):
    from contextlib import contextmanager
    from fastapi.testclient import TestClient
    from hub.app import create_app
    from hub.keyring import rotate_key

    with contextmanager(api.__wrapped__)(tmp_path, monkeypatch) as (app, client, _):
        cid, old, gid = old_connection(app, client, monkeypatch, unlimited=True)
        migrate(app)
        expected = refresh(client, cid, old["refresh_token"]).json()
        directory = app.state.store.directory
    if rekey:
        assert rotate_key(directory)["status"] == "completed"
    restarted = create_app(str(directory))
    with TestClient(restarted) as client:
        assert refresh(client, cid, old["refresh_token"]).json() == expected
        assert rpc(client, old["access_token"], PING).status_code == 200
        assert rpc(client, expected["access_token"], PING).status_code == 200
        assert grant(restarted, expected["access_token"])["id"] == gid


def test_independent_connections_serialize_migration_result(api, monkeypatch):
    from hub.oauth_lifetime import issue
    from hub.store import Store

    app, client, _ = api
    _, _, gid = old_connection(app, client, monkeypatch, unlimited=True)
    migrate(app)
    stores = [Store(app.state.store.directory) for _ in range(4)]
    def exchange(store):
        with store.transaction():
            return issue(store, gid)["access_token"]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(exchange, stores))
        assert len(set(results)) == 1
        assert rpc(client, results[0], PING).status_code == 200
    finally:
        for store in stores:
            store.close()


def test_failed_issue_rolls_back_without_consuming_legacy_alias(api, monkeypatch, caplog):
    import logging

    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch)
    migrate(app)
    store = app.state.store
    before = store.all("SELECT * FROM tokens WHERE grant_id=? ORDER BY hash", (gid,))
    store.execute("""CREATE TRIGGER fixture_issue_failure BEFORE INSERT ON oauth_lifetime_credentials
        BEGIN SELECT RAISE(ABORT, 'injected issue failure'); END""")
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    caplog.clear()
    failed = refresh(client, cid, old["refresh_token"])
    assert failed.status_code == 409
    assert "injected issue failure" not in failed.text
    messages = [r.getMessage() for r in caplog.records if r.getMessage().startswith("oauth.token ")]
    assert messages == ["oauth.token flow=refresh_token outcome=exchange_exception"]
    assert store.all("SELECT * FROM tokens WHERE grant_id=? ORDER BY hash", (gid,)) == before
    assert not store.one("SELECT * FROM oauth_lifetime_credentials WHERE grant_id=?", (gid,))
    store.execute("DROP TRIGGER fixture_issue_failure")
    assert refresh(client, cid, old["refresh_token"]).status_code == 200


def test_interrupted_upgrade_rolls_back_then_retries_atomically(api, monkeypatch):
    import sqlite3

    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch, unlimited=True)
    store = app.state.store
    before = store.all("SELECT * FROM tokens WHERE grant_id=? ORDER BY hash", (gid,))
    store.execute("""CREATE TRIGGER fixture_migration_failure BEFORE INSERT ON oauth_refresh_migrations
        BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected migration failure"):
        migrate(app)
    assert store.all("SELECT * FROM tokens WHERE grant_id=? ORDER BY hash", (gid,)) == before
    assert not store.one("SELECT * FROM oauth_lifetime_grants WHERE grant_id=?", (gid,))
    store.execute("DROP TRIGGER fixture_migration_failure")
    migrate(app)
    migrate(app)
    assert refresh(client, cid, old["refresh_token"]).status_code == 200


def test_expired_old_access_is_not_revived_by_upgrade_or_expiration_edit(api, monkeypatch):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch)
    store = app.state.store
    store.execute("UPDATE tokens SET expires=? WHERE hash=?",
                  (time.time() - 1, digest(old["access_token"])))
    migrate(app)
    assert rpc(client, old["access_token"], PING).status_code == 401
    response = client.put("/api/grants/" + gid + "/expiration",
                          json={"expires": None, "expected_revision": 1})
    assert response.status_code == 200, response.text
    assert rpc(client, old["access_token"], PING).status_code == 401
    assert refresh(client, cid, old["refresh_token"]).status_code == 200


def test_finite_deadline_ends_old_access_alias_and_stable_credential(api, monkeypatch):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch)
    migrate(app)
    new = refresh(client, cid, old["refresh_token"]).json()
    now = time.time()
    response = client.put("/api/grants/" + gid + "/expiration",
                          json={"expires": now + 120, "expected_revision": 1})
    assert response.status_code == 200, response.text
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: now + 121)
        assert rpc(client, old["access_token"], PING).status_code == 401
        assert rpc(client, new["access_token"], PING).status_code == 401
        assert refresh(client, cid, old["refresh_token"]).status_code == 400


@pytest.mark.parametrize("unlimited", [False, True])
def test_legacy_client_can_save_every_refresh_response(api, monkeypatch, unlimited):
    app, client, _ = api
    cid, old, gid = old_connection(app, client, monkeypatch, unlimited=unlimited)
    migrate(app)
    original = grant(app, old["access_token"])
    credentials = old
    first = None
    real_time = time.time
    for delay in (0, 3600, 3 * 86400):
        with monkeypatch.context() as patch:
            patch.setattr(time, "time", lambda: real_time() + delay)
            response = refresh(client, cid, credentials["refresh_token"])
            assert response.status_code == 200, response.text
            credentials = response.json()  # Strict clients replace their saved response.
            assert credentials["refresh_token"] == old["refresh_token"]
            assert grant(app, credentials["access_token"]) == original
            assert rpc(client, credentials["access_token"], PING).status_code == 200
            if unlimited:
                assert "expires_in" not in credentials
            elif first:
                assert 0 < credentials["expires_in"] < first["expires_in"]
            if first:
                assert credentials["access_token"] == first["access_token"]
            else:
                first = credentials


def test_reauthorize_registered_client_without_deleting_connection(api, monkeypatch):
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlsplit

    app, client, _ = api
    cid, old, _ = old_connection(app, client, monkeypatch, unlimited=True)
    migrate(app)
    verifier = "reauthorize-verifier-" * 4
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": cid,
              "redirect_uri": "http://localhost:12345/callback",
              "code_challenge_method": "S256", "code_challenge": challenge,
              "resource": "http://testserver/mcp"}
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    decision = client.post(f"/api/oauth/requests/{request_id}/decide", json={
        "allow": True, "scopes": ["read"], "projects": ["project"]})
    assert decision.status_code == 200, decision.text
    code = parse_qs(urlsplit(decision.json()["redirect"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "client_id": cid,
            "redirect_uri": params["redirect_uri"], "code_verifier": verifier}
    assert client.post("/oauth/token", data={**form, "code_verifier": "x" * 64}).status_code == 400
    result = client.post("/oauth/token", data=form)
    assert result.status_code == 200, result.text
    assert "refresh_token" not in result.json()
    assert rpc(client, result.json()["access_token"], PING).status_code == 200
    assert refresh(client, cid, old["refresh_token"]).status_code == 200
    assert rpc(client, old["access_token"], PING).status_code == 200
    assert client.post("/oauth/token", data=form).status_code == 400


def test_token_diagnostics_are_bounded_and_credential_free(api, monkeypatch, caplog):
    import logging
    from hub.oauth import OAuth

    app, client, _ = api
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    cid, old, _ = old_connection(app, client, monkeypatch, unlimited=True)
    migrate(app)
    result = refresh(client, cid, old["refresh_token"])
    assert result.status_code == 200
    assert refresh(client, "wrong", old["refresh_token"]).status_code == 400
    assert refresh(client, cid, old["refresh_token"], scope="execute").status_code == 400
    assert client.post("/oauth/token", data={"grant_type": "secret-unsupported-flow"}).status_code == 400
    assert client.post("/oauth/token", data={"grant_type": "authorization_code",
        "code": "secret-code", "code_verifier": "secret-verifier"}).status_code == 400
    messages = [r.getMessage() for r in caplog.records if r.getMessage().startswith("oauth.token ")]
    assert "oauth.token flow=authorization_code status=200 outcome=issued" in messages
    assert "oauth.token flow=refresh_token status=200 outcome=legacy_refreshed" in messages
    assert "oauth.token flow=refresh_token status=400 outcome=invalid_grant" in messages
    assert "oauth.token flow=refresh_token status=400 outcome=invalid_scope" in messages
    assert "oauth.token flow=authorization_code status=400 outcome=invalid_grant" in messages
    assert "oauth.token flow=unsupported status=400 outcome=unsupported_grant_type" in messages
    for secret in (cid, old["access_token"], old["refresh_token"], result.json()["access_token"],
                   "secret-code", "secret-verifier", "secret-unsupported-flow"):
        assert secret not in "\n".join(messages)
    caplog.clear()
    def fail_issue(self, grant_id):
        raise RuntimeError("secret-database-exception")
    monkeypatch.setattr(OAuth, "new_tokens", fail_issue)
    with pytest.raises(RuntimeError, match="secret-database-exception"):
        refresh(client, cid, old["refresh_token"])
    messages = [r.getMessage() for r in caplog.records if r.getMessage().startswith("oauth.token ")]
    assert messages == ["oauth.token flow=refresh_token outcome=exchange_exception"]
