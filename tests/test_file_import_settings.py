"""Persistent, fail-closed Hub ingress policy and authenticated review protocol."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from hub.file_import_settings import (
    ENVIRONMENT, META_KEY, FileImportPatch, initialize, snapshot, source_policy,
)
from hub.incoming_files import ingress_enabled
from hub.native_file_ingress import native_ingress_enabled, native_source_policy, inspect_native_file
from hub.store import Store
from shared.util import DevError
from tests.test_iam_integration import team as team

BASE = "/api/settings/file-import"


@pytest.fixture(autouse=True)
def clean_ingress_environment(monkeypatch):
    for key in ENVIRONMENT.values():
        monkeypatch.delenv(key, raising=False)


def reviewed(browser, patch, revision=None):
    revision = revision or browser.get(BASE).json()["revision"]
    body = {"expected_revision": revision, "patch": patch}
    result = browser.post(BASE + "/preview", json=body)
    assert result.status_code == 200, result.text
    return {**body, "confirmation": result.json()["confirmation"]}, result.json()


def saved(browser, patch):
    body, _ = reviewed(browser, patch)
    response = browser.put(BASE, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_new_defaults_keep_reviewed_sources_and_empty_providers(team):
    app, b = team
    result = b["owner"].get(BASE).json()
    assert all(result["settings"][key]["effective_value"] is True
               for key in ("streaming_enabled", "native_relay_enabled"))
    assert result["settings"]["streaming_enabled"]["source"] == "default"
    assert result["settings"]["streaming_enabled"]["configured_value"] is None
    assert result["source_policy"]["file_source_providers"] == []
    assert result["provider_options"] == ["openai_sediment"]
    assert ingress_enabled(app.state.store)
    assert native_ingress_enabled(app.state.store)


def test_admin_auth_csrf_and_no_generic_settings(team):
    app, b = team
    body, _ = reviewed(b["owner"], {"streaming_enabled": False})
    assert b["alice"].get(BASE).status_code == 403
    assert b["alice"].post(BASE + "/preview", json={k: v for k, v in body.items() if k != "confirmation"}).status_code == 403
    assert b["alice"].put(BASE, json=body).status_code == 403
    assert b["owner"].put(BASE, json=body, headers={"X-RD-CSRF": "bad"}).status_code == 403
    assert b["owner"].post(BASE + "/preview", json={k: v for k, v in body.items() if k != "confirmation"},
                           headers={"X-RD-CSRF": ""}).status_code == 403
    assert b["owner"].put(BASE, json=body, headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert b["owner"].client.get(BASE).status_code == 401
    before = snapshot(app.state.store)
    app.state.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    assert b["owner"].put(BASE, json=body).status_code == 403
    assert snapshot(app.state.store) == before


@pytest.mark.parametrize("patch", [
    {}, {"unreviewed": True}, {"streaming_enabled": "true"}, {"native_relay_enabled": 1},
    {"native_file_hosts": "files.oaiusercontent.com"},
    {"native_file_hosts": ["*.oaiusercontent.com"]},
    {"native_file_hosts": ["https://files.oaiusercontent.com/private?token=secret"]},
    {"native_file_hosts": ["127.0.0.1"]},
    {"native_file_hosts": ["files.oaiusercontent.com:443"]},
    {"native_file_hosts": [False]},
    {"native_file_providers": ["openai"]},
    {"native_file_providers": ["all"]},
    {"native_file_providers": None, "shell": {"enabled": True}},
])
def test_invalid_typed_values_do_not_write(team, patch):
    app, b = team
    initial = snapshot(app.state.store)
    response = b["owner"].post(BASE + "/preview", json={"expected_revision": initial["revision"], "patch": patch})
    assert response.status_code == 422
    assert snapshot(app.state.store) == initial
    assert not app.state.store.all("SELECT * FROM audit WHERE action='settings.file_import.updated'")


def test_stale_write_exact_confirmation_and_env_change(team, monkeypatch):
    app, b = team
    body, review = reviewed(b["owner"], {"streaming_enabled": False})
    assert review["snapshot"]["settings"]["native_relay_enabled"]["effective_value"] is False
    wrong = {**body, "confirmation": "0" * 64}
    assert b["owner"].put(BASE, json=wrong).status_code == 409
    changed = {**body, "patch": {"native_relay_enabled": False}}
    assert b["owner"].put(BASE, json=changed).status_code == 409
    assert ingress_enabled(app.state.store)
    assert b["owner"].put(BASE, json=body).status_code == 200
    assert b["owner"].put(BASE, json=body).status_code == 409
    assert not ingress_enabled(app.state.store)
    next_body, _ = reviewed(b["owner"], {"streaming_enabled": True})
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_PROVIDERS", "[]")
    assert b["owner"].put(BASE, json=next_body).status_code == 409
    assert not ingress_enabled(app.state.store)


def test_false_empty_inherit_precedence_restart_and_normalization(team, monkeypatch):
    app, b = team
    monkeypatch.setenv("CODEPIER_FILE_IMPORT_STREAMING", "false")
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_HOSTS", '["FILES.OAIUSERCONTENT.COM."]')
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_PROVIDERS", '["openai_sediment"]')
    assert not ingress_enabled(app.state.store)
    assert native_source_policy(app.state.store)["allowed_hosts"] == ["files.oaiusercontent.com"]
    result = saved(b["owner"], {"streaming_enabled": True, "native_file_hosts": [], "native_file_providers": []})
    assert ingress_enabled(app.state.store)
    assert result["source_policy"]["allowed_hosts"] == []
    assert result["source_policy"]["file_source_providers"] == []
    second = Store(app.state.store.directory)
    try:
        initialize(second)
        assert snapshot(second) == result
    finally:
        second.close()
    body, review = reviewed(b["owner"], {"streaming_enabled": None, "native_file_hosts": None})
    assert review["changes"][0]["reset_to_inherit"] is True
    assert review["snapshot"]["settings"]["streaming_enabled"]["effective_value"] is False
    assert review["snapshot"]["source_policy"]["allowed_hosts"] == ["files.oaiusercontent.com"]
    reset = b["owner"].put(BASE, json=body).json()
    assert reset["settings"]["streaming_enabled"]["configured_value"] is None
    assert reset["settings"]["streaming_enabled"]["source"] == "environment"
    assert reset["source_policy"]["file_source_providers"] == []


@pytest.mark.parametrize("name,raw", [
    ("CODEPIER_FILE_IMPORT_STREAMING", "TRUE"),
    ("CODEPIER_NATIVE_FILE_RELAY", "1"),
    ("CODEPIER_NATIVE_FILE_HOSTS", "secret-url-payload"),
    ("CODEPIER_NATIVE_FILE_PROVIDERS", '["secret-provider"]'),
])
def test_invalid_environment_fails_closed_and_is_redacted(team, monkeypatch, name, raw):
    app, b = team
    monkeypatch.setenv(name, raw)
    result = b["owner"].get(BASE)
    key = next(key for key, env in ENVIRONMENT.items() if env == name)
    setting = result.json()["settings"][key]
    assert setting["state"] == "invalid"
    assert setting["effective_value"] in (False, [])
    if len(raw) > 4:
        assert raw not in result.text
    if not name.endswith(("STREAMING", "RELAY")):
        with pytest.raises(DevError, match="source policy is invalid"):
            native_source_policy(app.state.store)


def test_legacy_meta_missing_and_corrupt_policy_never_fallback(team):
    app, b = team
    store = app.state.store
    store.execute("DELETE FROM meta WHERE key=?", (META_KEY,))
    initialize(store)
    assert snapshot(store)["settings"]["streaming_enabled"]["effective_value"] is True
    store.execute("UPDATE meta SET value=? WHERE key=?", ('{"secret":"do-not-echo"}', META_KEY))
    initialize(store)
    result = b["owner"].get(BASE)
    assert "do-not-echo" not in result.text
    assert not ingress_enabled(store)
    assert not native_ingress_enabled(store)
    assert result.json()["source_policy"] is None
    assert all(value["state"] == "invalid" for value in result.json()["settings"].values())


def test_saved_gates_enforce_current_runtime_without_widening_authority(team):
    app, b = team
    store = app.state.store
    prior = {table: store.all("SELECT * FROM " + table) for table in ("projects", "grants", "memberships", "devices")}
    saved(b["owner"], {"streaming_enabled": False})
    response = b["owner"].post("/api/file-imports", json={
        "project": "project-team", "path": "out.bin", "size": 0,
        "sha256": "0" * 64, "idempotency_key": "test-saved-gate"})
    assert response.status_code == 404
    principal = SimpleNamespace(admin=True)
    with pytest.raises(DevError) as caught:
        app.state.runtime.integrations.guard("incoming_upload_begin", {}, {}, principal)
    assert caught.value.code == "FILE_IMPORT_DISABLED"
    saved(b["owner"], {"streaming_enabled": True, "native_relay_enabled": False,
                       "native_file_hosts": [], "native_file_providers": []})
    assert ingress_enabled(store) and not native_ingress_enabled(store)
    for table, before in prior.items():
        assert store.all("SELECT * FROM " + table) == before
    audits = store.all("SELECT detail FROM audit WHERE action='settings.file_import.updated'")
    assert audits and all(set(json.loads(row["detail"])) == {"settings", "revision", "activation"} for row in audits)


def test_source_preview_uses_saved_policy_and_never_echoes_source_payload(team, monkeypatch):
    app, b = team
    saved(b["owner"], {"native_file_hosts": [], "native_file_providers": ["openai_sediment"]})
    owner = app.state.auth
    # Resolve the normal panel principal through the authenticated HTTP settings-independent endpoint.
    from fastapi import Request
    from hub.db_worker import database_endpoint
    @app.get("/test/file-source-preview")
    async def preview_source(request: Request):
        principal = await app.state.store.run(owner.panel, request)
        return await inspect_native_file(app.state.runtime, {
            "project": "project-team", "idempotency_key": "preview-policy-fixture", "file": {"file_id": "private-file-id",
            "download_url": "https://sdmntprwest.oaiusercontent.com/private/path?sig=supersecret"}},
            principal)
    result = b["owner"].get("/test/file-source-preview")
    assert result.status_code == 200, result.text
    assert result.json()["source_allowed"] is True
    assert result.json()["file_source_providers"] == ["openai_sediment"]
    assert result.json()["allowed_hosts"] == []
    assert result.json()["request_sent"] is False
    assert all(secret not in result.text for secret in ("private-file-id", "private/path", "supersecret"))
    saved(b["owner"], {"native_file_providers": []})
    result = b["owner"].get("/test/file-source-preview")
    assert result.json()["source_allowed"] is False
    assert result.json()["approval_required"] is True


def test_admin_revoked_between_outer_and_transaction_check(team, monkeypatch):
    app, b = team
    body, _ = reviewed(b["owner"], {"streaming_enabled": False})
    original = app.state.auth.instance
    count = 0

    def checked(request, write=False):
        nonlocal count
        count += 1
        principal = original(request, write)
        if count == 1:
            app.state.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
        return principal

    monkeypatch.setattr(app.state.auth, "instance", checked)
    response = b["owner"].put(BASE, json=body)
    assert response.status_code == 403
    assert ingress_enabled(app.state.store)
    assert not app.state.store.all("SELECT * FROM audit WHERE action='settings.file_import.updated'")


def test_startup_initializes_legacy_database_without_overwriting_policy(tmp_path, monkeypatch):
    from hub.app import create_app
    monkeypatch.setenv("HUB_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    directory = tmp_path / "legacy"
    old = Store(directory)
    assert old.one("SELECT value FROM meta WHERE key=?", (META_KEY,)) is None
    old.close()
    app = create_app(str(directory))
    try:
        record = app.state.store.one("SELECT value FROM meta WHERE key=?", (META_KEY,))
        assert json.loads(record["value"]) == {"version": 1, "revision": 0, "values": {}}
        app.state.store.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps({
            "version": 1, "revision": 4, "values": {"streaming_enabled": False, "native_file_hosts": []}}), META_KEY))
    finally:
        # Use lifespan teardown to release the normal instance lock and workers.
        from fastapi.testclient import TestClient
        with TestClient(app):
            pass
    reopened = create_app(str(directory))
    from fastapi.testclient import TestClient
    with TestClient(reopened):
        current = snapshot(reopened.state.store)
        assert current["settings"]["streaming_enabled"]["configured_value"] is False
        assert current["source_policy"]["allowed_hosts"] == []
