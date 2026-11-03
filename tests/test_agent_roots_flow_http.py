"""Actual panel/Agent authorization flow using only disposable fixture paths."""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from tests.support import running_stack, wait_for


@pytest.fixture(scope="module")
def authorization_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp("agent-roots-http")) as stack:
        yield stack


def endpoint(stack):
    return f"/api/devices/{stack.device}/roots"


def completed(stack, result):
    if result.get("operation_id"):
        row = stack.poll(result["operation_id"], timeout=40)
        assert row["state"] == "succeeded", row
        assert row["result"]["ok"], row
        return row["result"]["data"]
    return result


def snapshot(stack):
    result = stack.must(stack.client.get(endpoint(stack)))
    if result.get("pending"):
        result = completed(stack, result)
    assert result["device_id"] == stack.device
    return result


def applied(stack, expected_sha256):
    def check():
        result = snapshot(stack)
        return result if result.get("state") == "applied" and result.get("config_sha256") == expected_sha256 else None
    return wait_for(check, timeout=40)


def change_body(stack, path, *, action="upsert", writable=True, allow_tasks=True):
    before = snapshot(stack)
    assert before["state"] == "applied", before
    return {
        "action": action,
        "path": str(path),
        "writable": writable,
        "allow_tasks": allow_tasks,
        "expected_sha256": before["config_sha256"],
        "idempotency_key": "roots-http-" + uuid.uuid4().hex,
    }


def save(stack, body):
    receipt = stack.must(stack.client.post(endpoint(stack), json=body))
    result = completed(stack, receipt)
    current = applied(stack, result["config_sha256"])
    return receipt, current


def test_panel_authorize_map_write_readonly_and_revoke(authorization_stack):
    s = authorization_stack
    target = s.directory / "Shared 文件" / "Projects"
    target.mkdir(parents=True)
    original = json.loads(s.config_path.read_text())
    body = change_body(s, target)
    preview = completed(s, s.must(s.client.post(endpoint(s) + "/preview", json=body)))
    assert preview.get("valid", True)
    assert json.loads(s.config_path.read_text()) == original

    receipt, current = save(s, body)
    raw = json.loads(s.config_path.read_text())
    added = [root for root in raw["allowed_roots"] if root["path"] == str(target.resolve())]
    assert added == [{"path": str(target.resolve()), "writable": True, "allow_tasks": True}]
    preserved = [root for root in raw["allowed_roots"] if root["path"] != str(target.resolve())]
    assert sorted(preserved, key=lambda root: root["path"]) == sorted(original["allowed_roots"], key=lambda root: root["path"])
    assert {key: value for key, value in raw.items() if key != "allowed_roots"} == {
        key: value for key, value in original.items() if key != "allowed_roots"
    }
    assert hashlib.sha256(s.config_path.read_bytes()).hexdigest() == current["config_sha256"]

    replay = s.must(s.client.post(endpoint(s), json=body))
    assert replay["operation_id"] == receipt["operation_id"]
    recovered = s.must(s.client.get(endpoint(s) + "/requests/" + body["idempotency_key"]))
    assert recovered["operation_id"] == receipt["operation_id"]
    assert json.loads(s.config_path.read_text()) == raw

    mapped = s.create_project({
        "alias": "NewlyAuthorized", "device_id": s.device, "root": str(target),
        "description": "Disposable directory authorization acceptance", "mode": "write", "allow_tasks": True,
    })
    denied = s.mcp("read", {"project": mapped["alias"], "path": "proof.txt"})
    assert denied["isError"], "A directory grant must not expand the existing MCP project grant"
    write = s.call("fs_write", {"project": mapped["id"], "path": "proof.txt", "content": "授权闭环\n",
        "expected_sha256": "new", "idempotency_key": uuid.uuid4().hex})
    completed(s, write)
    read = completed(s, s.call("fs_read", {"project": mapped["id"], "path": "proof.txt"}))
    assert read["content"] == "授权闭环\n"

    save(s, change_body(s, target, writable=False, allow_tasks=False))
    denied = s.call("fs_write", {"project": mapped["id"], "path": "blocked.txt", "content": "must not write",
        "expected_sha256": "new", "idempotency_key": uuid.uuid4().hex}, raw=True)
    assert denied.status_code in {403, 409}, denied.text
    assert denied.json()["error"]["code"] == "READ_ONLY", denied.text
    assert not (target / "blocked.txt").exists()

    save(s, change_body(s, target, action="remove", writable=False, allow_tasks=False))
    assert (target / "proof.txt").read_text() == "授权闭环\n"
    assert not any(root["path"] == str(target.resolve()) for root in snapshot(s)["roots"])
    denied = s.call("fs_read", {"project": mapped["id"], "path": "proof.txt"}, raw=True)
    assert denied.status_code in {403, 409}, denied.text
    assert denied.json()["error"]["code"] == "ROOT_NOT_ALLOWED", denied.text
    assert json.loads(s.config_path.read_text())["allowed_roots"] == original["allowed_roots"]


def test_stale_configuration_never_overwrites_a_later_root_change(authorization_stack):
    s = authorization_stack
    first, second = s.directory / "first-grant", s.directory / "stale-grant"
    first.mkdir(); second.mkdir()
    current = change_body(s, first)
    stale = {**current, "path": str(second), "idempotency_key": "roots-http-" + uuid.uuid4().hex}
    save(s, current)
    preserved = s.config_path.read_bytes()
    result = s.client.post(endpoint(s), json=stale)
    if result.is_success:
        value = result.json()
        assert value.get("operation_id"), value
        failed = s.poll(value["operation_id"], timeout=40)
        assert failed["state"] == "failed", failed
        assert failed["result"]["error"], failed
    else:
        assert result.status_code == 409, result.text
    assert s.config_path.read_bytes() == preserved
    assert not any(root["path"] == str(second.resolve()) for root in snapshot(s)["roots"])
    save(s, change_body(s, first, action="remove"))


def test_csrf_and_public_mcp_cannot_authorize_paths(authorization_stack):
    s = authorization_stack
    path = s.directory / "must-not-be-granted"
    path.mkdir()
    before = s.config_path.read_bytes()
    body = change_body(s, path)
    for suffix in ("", "/preview"):
        response = s.client.post(endpoint(s) + suffix, json=body, headers={"X-RD-CSRF": "wrong"})
        assert response.status_code == 403, response.text
    catalog = s.rpc("tools/list").json()["result"]["tools"]
    assert not any("agent_roots" in item["name"] or "authorized_roots" in item["name"] for item in catalog)
    for name in ("agent_roots_get", "agent_roots_preview", "agent_roots_set", "agent_roots_update"):
        assert s.mcp(name, {"project": "ProjectAlpha", **body})["isError"]
    assert s.config_path.read_bytes() == before


def test_offline_configuration_is_not_presented_as_applied(authorization_stack):
    s = authorization_stack
    before = s.config_path.read_bytes()
    body = change_body(s, s.directory / "offline-grant")
    s.stop_agent()
    try:
        offline = wait_for(lambda: (lambda result: result if result.get("state") == "offline" else None)(snapshot(s)), timeout=20)
        assert offline["state"] == "offline"
        response = s.client.post(endpoint(s), json=body)
        assert response.status_code == 409, response.text
        assert s.config_path.read_bytes() == before
    finally:
        s.start_agent()
