"""Boundary and failure-injection regressions for shared code and local CLI."""
import base64
import json
import os
import sqlite3
import sys
import zipfile

import pytest
from pydantic import ValidationError

from hub.__main__ import main, write_backup
from hub.store import Store
from hub.runtime import Runtime
from shared.contracts import TOOLS
from shared.crypto import SecureChannel, password_hash, token
from shared.util import normalize_url, valid_json_value


@pytest.mark.parametrize("value", ["\ud800", {"x": [float("nan")]}, {"\udfff": "x"}, [float("inf")], {1: "x"}])
def test_json_boundary_rejects_unserializable_values(value):
    assert not valid_json_value(value)


def test_json_boundary_limits_nesting_without_recursing():
    value = []
    value.append(value)
    assert not valid_json_value(value)
    assert valid_json_value({"hello": "世界\U0001f642", "args": [True, None, 1.2, 1]})


@pytest.mark.parametrize("name,args", [
    ("fs_read", {"project": "test", "path": "\ud800"}),
    ("operations_list", {"before_created": float("nan")}),
    ("operations_list", {"before_created": float("inf")}),
])
def test_invalid_values_rejected_before_persistence(name, args):
    with pytest.raises(ValidationError):
        TOOLS[name].model.model_validate(args)


@pytest.mark.parametrize("value", ["", "   ", "http://ho\nst", "http://host\x00", "http://bad host", "http://host\\evil", "http://\ud800", None])
def test_bad_hub_url_does_not_silently_change_or_break_reconnect(value):
    with pytest.raises(ValueError):
        normalize_url(value)


def test_idn_public_url_is_safe_in_oauth_challenge(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from hub.app import create_app
    monkeypatch.setenv("HUB_PUBLIC_URL", "https://例子.测试:8443")
    monkeypatch.setenv("MCP_PUBLIC_URL", "")
    with TestClient(create_app(str(tmp_path / "hub"))) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert response.status_code == 401
        assert 'https://xn--fsqu00a.xn--0zwm56d:8443/' in response.headers["www-authenticate"]


def test_failed_pack_does_not_desynchronize_channel():
    secret, challenge = token(), token()
    agent = SecureChannel(secret, challenge, "device", "agent")
    hub = SecureChannel(secret, challenge, "device", "hub")
    with pytest.raises(ValueError):
        agent.pack({"value": "\ud800"})
    with pytest.raises(ValueError):
        agent.pack({"value": "x" * (13 * 1024 * 1024)})
    assert agent.sent == 0
    assert hub.unpack(agent.pack({"type": "heartbeat"})) == {"type": "heartbeat"}


def test_channel_depth_limit_matches_received_envelope():
    secret, challenge = token(), token()
    sender = SecureChannel(secret, challenge, "device", "agent")
    receiver = SecureChannel(secret, challenge, "device", "hub")
    value = 0
    for _ in range(99):
        value = {"v": value}
    with pytest.raises(ValueError):
        sender.pack({"type": "probe", "value": value})
    body = {"type": "probe", "value": value["v"]}
    assert receiver.unpack(sender.pack(body)) == body


@pytest.mark.parametrize("clear", [b"[]", b'{"seq":1,"body":{"bad":NaN}}', b'{"seq":1,"body":{"bad":"\\ud800"}}'])
def test_malformed_authenticated_packet_rejected_without_consuming_sequence(clear):
    secret, challenge = token(), token()
    sender = SecureChannel(secret, challenge, "device", "agent")
    receiver = SecureChannel(secret, challenge, "device", "hub")
    nonce = os.urandom(12)
    packet = base64.b64encode(nonce + sender.cipher.encrypt(nonce, clear, sender.prefix + b"agent")).decode()
    with pytest.raises(ValueError):
        receiver.unpack(packet)
    assert receiver.unpack(sender.pack({"ok": True})) == {"ok": True}


def test_failed_backup_leaves_no_published_partial_archive(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    output = tmp_path / "backups" / "hub.zip"
    original = zipfile.ZipFile.write
    def failing_write(archive, filename, *args, **kwargs):
        if str(filename).endswith("master.key"):
            raise OSError("injected full disk")
        return original(archive, filename, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, "write", failing_write)
    try:
        with pytest.raises(OSError, match="full disk"):
            write_backup(store, output)
        assert not output.exists()
        assert list(output.parent.iterdir()) == []
    finally:
        store.close()


def test_backup_exclusive_private_and_restorable(tmp_path):
    store = Store(tmp_path / "store")
    output = tmp_path / "hub.zip"
    try:
        write_backup(store, output)
        original = output.read_bytes()
        with pytest.raises(FileExistsError):
            write_backup(store, output)
        assert output.read_bytes() == original
        if os.name != "nt":
            assert output.stat().st_mode & 0o777 == 0o600
        with zipfile.ZipFile(output) as archive:
            assert archive.testzip() is None
            archive.extractall(tmp_path / "restored")
        restored = Store(tmp_path / "restored")
        try:
            assert restored.one("PRAGMA quick_check")["quick_check"] == "ok"
            assert restored.decrypt(store.encrypt("roundtrip")) == "roundtrip"
        finally:
            restored.close()
    finally:
        store.close()


def test_reset_password_rolls_back_when_revocation_fails(tmp_path, monkeypatch):
    directory = tmp_path / "store"
    store = Store(directory)
    old_hash = password_hash("previous-password")
    store.execute("INSERT INTO users VALUES ('owner','admin',?,0)", (old_hash,))
    store.execute("INSERT INTO sessions VALUES ('session','owner','csrf',9999999999)")
    from tests.legacy_iam_fixture import attach_session_security
    attach_session_security(store)
    store.execute("CREATE TRIGGER fail_revocation BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT, 'injected revoke failure'); END")
    monkeypatch.setenv("RD_ADMIN_PASSWORD", "replacement-password")
    monkeypatch.setattr(sys, "argv", ["hub", "--data-dir", str(directory), "reset-password"])
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected revoke failure"):
            main()
        assert store.one("SELECT password_hash FROM users")["password_hash"] == old_hash
        assert store.one("SELECT id_hash FROM sessions")["id_hash"] == "session"
    finally:
        store.close()


def test_unconfirmed_file_durability_is_marked_for_review(tmp_path):
    store = Store(tmp_path / "hub")
    try:
        store.execute("INSERT INTO operations(id,actor,tool,args_summary,fingerprint,state,created,updated) VALUES ('flush','panel:owner','fs_write','{}','hash','running',0,0)")
        runtime = Runtime(store)
        runtime.complete({"id": "flush"}, {"ok": False, "error": {"code": "DURABILITY_UNCONFIRMED", "message": "Directory sync failed after publication"}})
        operation = store.one("SELECT * FROM operations WHERE id='flush'")
        assert operation["state"] == "needs_review"
        assert operation["payload"] is None
        assert json.loads(operation["result"])["error"]["code"] == "DURABILITY_UNCONFIRMED"
    finally:
        store.close()
