"""Settings adapters reuse original stores, role guards and bounded node receipts."""
import asyncio
import json

import pytest

from hub.settings_snapshot import node_snapshot
from shared.settings_catalog import settings_catalog
from tests.test_audit_api import api as api


def items(response):
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["items"]}


def test_catalog_is_descriptive_copy_and_has_all_groups():
    first = settings_catalog()
    assert len(first["groups"]) == 8
    assert len(first["items"]) >= 40
    assert len({item["id"] for item in first["items"]}) == len(first["items"])
    first["items"][0]["title"] = "changed"
    assert settings_catalog()["items"][0]["title"] != "changed"
    assert all(item["scope"] and item["activation"] and item["fields"] for item in first["items"])


def test_effective_file_values_preserve_false_empty_and_unset(api, monkeypatch):
    _, client, _ = api
    for key in ("CODEPIER_FILE_IMPORT_STREAMING", "CODEPIER_NATIVE_FILE_RELAY",
                "CODEPIER_NATIVE_FILE_HOSTS", "CODEPIER_NATIVE_FILE_PROVIDERS"):
        monkeypatch.delenv(key, raising=False)
    initial = items(client.get("/api/settings/catalog"))
    assert initial["hub_ingress"]["source"] == "default"
    assert initial["hub_ingress"]["configured_value"] is None
    assert initial["hub_file_sources"]["effective_value"]["file_source_providers"] == []
    monkeypatch.setenv("CODEPIER_FILE_IMPORT_STREAMING", "false")
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_RELAY", "true")
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_HOSTS", "[]")
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_PROVIDERS", "[]")
    rows = items(client.get("/api/settings/catalog"))
    assert rows["hub_ingress"]["effective_value"] is False
    assert rows["hub_ingress"]["configured_value"] is None
    assert rows["hub_ingress"]["inherited_value"] is False
    assert rows["hub_ingress"]["inherited_source"] == "environment"
    assert rows["native_relay"]["effective_value"] is False
    assert rows["hub_file_sources"]["effective_value"]["allowed_hosts"] == []
    assert rows["hub_file_sources"]["effective_value"]["file_source_providers"] == []


@pytest.mark.parametrize("raw", ["1", "0", "yes", "on", "TRUE", ""])
def test_unrecognized_boolean_is_explicitly_invalid(api, monkeypatch, raw):
    _, client, _ = api
    monkeypatch.setenv("CODEPIER_FILE_IMPORT_STREAMING", raw)
    row = items(client.get("/api/settings/catalog"))["hub_ingress"]
    assert row["effective_value"] is False
    assert row["state"] == "invalid"
    assert row["configured_value"] is None
    assert row["inherited_state"] == "invalid"


def test_node_projection_rejects_arbitrary_nested_values():
    result = node_snapshot({"file_import": {"resumable_upload_enabled": "true",
        "allowed_hosts": [{"secret": "SECRET"}], "file_source_providers": ["SECRET"],
        "max_bytes": True, "policy_mode": {"secret": "SECRET"}},
        "execution": {"shell": {"enabled": {"secret": "SECRET"}, "max_timeout_seconds": "SECRET"}},
        "browser": {"connected": ["SECRET"]}, "checks": {"secret": "SECRET"}})
    assert "SECRET" not in json.dumps(result)
    assert result["values"]["node_ingress"] is None
    assert result["values"]["node_file_sources"] == {}


def test_invalid_source_policy_is_visible_not_echoed(api, monkeypatch):
    _, client, _ = api
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_HOSTS", "private-secret-do-not-echo")
    result = client.get("/api/settings/catalog")
    assert items(result)["hub_file_sources"]["state"] == "invalid"
    assert "private-secret-do-not-echo" not in result.text


def test_personal_settings_available_without_instance_admin(api):
    app, client, _ = api
    app.state.store.execute("UPDATE iam_users SET instance_admin=0 WHERE user_id='owner'")
    response = client.get("/api/settings/catalog")
    rows = items(response)
    assert rows["access_defaults"]["editable_here"] is True
    assert rows["appearance"]["editable_here"] is True
    assert rows["public_url"]["editable_here"] is False
    assert rows["hub_process"]["effective_value"] is None
    assert rows["hub_file_sources"]["effective_value"] is None
    assert str(app.state.store.directory) not in response.text
    assert client.put("/api/settings", json={"public_url": "https://forbidden.invalid"}).status_code == 403
    saved = client.put("/api/settings/access", json={"all_projects": False, "developer_scopes": True,
        "expected_defaults": {"all_projects": False, "developer_scopes": False}})
    assert saved.status_code == 200, saved.text
    assert items(client.get("/api/settings/catalog"))["access_defaults"]["effective_value"] == {
        "all_projects": False, "developer_scopes": True}


def test_missing_scope_is_404_and_unselected_or_offline_is_unknown(api):
    _, client, _ = api
    for endpoint in ("/api/settings/catalog", "/api/settings/node"):
        response = client.get(endpoint, params={"project": "not-authorized"})
        assert response.status_code == 404
        assert "/tmp/fixture" not in response.text
    assert client.get("/api/settings/catalog").json()["node"]["effective_value"] is None
    result = client.get("/api/settings/node", params={"project": "project"}).json()
    assert result["state"] == "offline" and result["values"] == {}


def test_public_url_conditional_update_and_legacy_compatibility(api):
    _, client, _ = api
    original = client.get("/api/settings").json()["public_url"]
    response = client.put("/api/settings", json={"public_url": "https://new.example.test",
        "expected_public_url": original})
    assert response.status_code == 200, response.text
    stale = client.put("/api/settings", json={"public_url": "https://stale.example.test",
        "expected_public_url": original})
    assert stale.status_code == 409
    assert client.get("/api/settings").json()["public_url"] == "https://new.example.test"
    # Replaying the exact successful update is harmless.
    assert client.put("/api/settings", json={"public_url": "https://new.example.test",
        "expected_public_url": original}).status_code == 200
    assert client.put("/api/settings", json={"public_url": original}).status_code == 200


def test_defaults_conditional_update_preserves_original_storage(api):
    app, client, _ = api
    baseline = {"all_projects": False, "developer_scopes": False}
    updated = {"all_projects": True, "developer_scopes": False}
    assert client.put("/api/settings/access", json={**updated, "expected_defaults": baseline}).status_code == 200
    assert client.put("/api/settings/access", json={"all_projects": False, "developer_scopes": True,
        "expected_defaults": baseline}).status_code == 409
    assert client.put("/api/settings/access", json={**updated, "expected_defaults": baseline}).status_code == 200
    record = app.state.store.one("SELECT value FROM meta WHERE key='mcp_access_defaults:owner'")
    assert json.loads(record["value"]) == updated
    assert "expected_defaults" not in record["value"]
    assert client.put("/api/settings/access", json={**baseline, "expected_defaults": {"all_projects": False}}).status_code == 422


def test_node_projection_does_not_emit_paths_environment_or_secrets():
    raw = {"file_import": {"resumable_upload_enabled": False, "allowed_hosts": [], "file_source_providers": [],
                          "max_bytes": 10, "signed_url": "SECRET"},
           "execution": {"project_root": "/private/SECRET", "shell": {"enabled": False,
                          "env": {"AUTH": "SECRET"}, "command": ["/private/SECRET"]}},
           "browser": {"enabled": False, "connected": False, "profile_id": "SECRET"},
           "local_control": False, "secret": "SECRET"}
    result = node_snapshot(raw)
    assert "SECRET" not in json.dumps(result)
    assert result["values"]["node_ingress"] is False
    assert result["values"]["node_file_sources"]["allowed_hosts"] == []
    assert result["values"]["local_control"] is False
    assert node_snapshot({})["state"] == "unknown"
    assert node_snapshot({"pending": True, "operation_id": "receipt"})["operation_id"] == "receipt"


def test_node_read_is_coalesced_and_completed_value_cached(api, monkeypatch):
    app, client, _ = api
    calls = []
    monkeypatch.setattr(app.state.runtime, "online", lambda _: True)
    async def invoke(name, args, principal):
        calls.append((name, args))
        await asyncio.sleep(.01)
        return {"file_import": {"resumable_upload_enabled": False}, "local_control": False}
    monkeypatch.setattr(app.state.runtime, "invoke", invoke)
    for _ in range(3):
        response = client.get("/api/settings/node", params={"project": "project"})
        assert response.status_code == 200, response.text
        assert response.json()["values"]["node_ingress"] is False
    assert calls == [("readiness_get", {"project": "project"})]


def test_pending_node_check_resumes_original_receipt(api, monkeypatch):
    app, client, _ = api
    calls, reads = [], []
    monkeypatch.setattr(app.state.runtime, "online", lambda _: True)
    async def invoke(name, args, principal):
        calls.append(name)
        return {"pending": True, "operation_id": "original-receipt"}
    def operation_row(identifier, principal):
        reads.append(identifier)
        return {"id": identifier, "tool": "readiness_get", "project_id": "project", "device_id": "device"}
    def operation(identifier, principal, view):
        return {"operation_id": identifier, "pending": False,
                "result": {"ok": True, "data": {"file_import": {"resumable_upload_enabled": True}}}}
    monkeypatch.setattr(app.state.runtime, "invoke", invoke)
    monkeypatch.setattr(app.state.runtime, "operation_row", operation_row)
    monkeypatch.setattr(app.state.runtime, "operation", operation)
    first = client.get("/api/settings/node", params={"project": "project"}).json()
    assert first["state"] == "pending" and first["operation_id"] == "original-receipt"
    second = client.get("/api/settings/node", params={"project": "project"}).json()
    assert second["state"] == "checked" and second["values"]["node_ingress"] is True
    assert calls == ["readiness_get"] and reads == ["original-receipt"]
