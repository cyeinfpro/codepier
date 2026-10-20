"""Login admission and session/CSRF boundaries from the 2026-10-20 report."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest
from fastapi import Request

import hub.auth as auth_module
from hub.auth import Auth
from hub.store import Store
from shared.crypto import digest, password_hash
from shared.util import DevError


def request(ip="192.0.2.1", *, cookie="", csrf="", origin="http://testserver"):
    headers = [(b"origin", origin.encode())]
    if cookie:
        headers.append((b"cookie", ("rd_session=" + cookie).encode()))
    if csrf:
        headers.append((b"x-rd-csrf", csrf.encode()))
    return Request({"type": "http", "scheme": "http", "server": ("testserver", 80),
                    "path": "/api/login", "query_string": b"", "headers": headers,
                    "client": (ip, 12345), "method": "POST"})


@pytest.fixture
def auth(tmp_path):
    store = Store(tmp_path / "hub")
    store.execute("INSERT INTO users VALUES (?,?,?,?)",
                  ("owner", "owner", password_hash("correct-password"), 1))
    value = Auth(store)
    try:
        yield value
    finally:
        store.close()


@pytest.mark.parametrize("username", ["owner", "missing-user"])
def test_distributed_failed_passwords_share_an_account_budget(auth, username):
    async def exercise():
        for i in range(8):
            with pytest.raises(DevError) as failed:
                await auth.login(request(f"192.0.2.{i + 1}"), username, "wrong-password")
            assert failed.value.code == "LOGIN_FAILED"
        with pytest.raises(DevError) as throttled:
            await auth.login(request("198.51.100.1"), username, "wrong-password")
        assert throttled.value.code == "LOGIN_THROTTLED"
        assert not auth.store.one("SELECT 1 AS present FROM sessions")
    asyncio.run(exercise())


def test_ip_budget_still_applies_when_usernames_change(auth):
    for i in range(8):
        auth._login_attempt(request(), f"different-user-{i}")
    with pytest.raises(DevError) as throttled:
        auth._login_attempt(request(), "another-user")
    assert throttled.value.code == "LOGIN_THROTTLED"


def test_rejected_probes_do_not_extend_the_account_window(auth, monkeypatch):
    clock = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(auth_module, "time", SimpleNamespace(time=lambda: clock.value))
    for i in range(8):
        auth._login_attempt(request(f"192.0.2.{i + 1}"), "owner")
    clock.value = 1299.0
    with pytest.raises(DevError):
        auth._login_attempt(request("198.51.100.1"), "owner")
    clock.value = 1300.001
    _, user = auth._login_attempt(request("198.51.100.1"), "owner")
    assert user["id"] == "owner"


def test_account_budget_survives_restart_and_is_atomic_across_stores(auth):
    second_store = Store(auth.store.directory)
    second = Auth(second_store)
    start = threading.Barrier(16)
    def attempt(index):
        start.wait(timeout=10)
        try:
            (auth if index % 2 else second)._login_attempt(
                request(f"192.0.2.{index + 1}"), "owner")
            return "admitted"
        except DevError as exc:
            return exc.code
    try:
        with ThreadPoolExecutor(max_workers=16) as workers:
            outcomes = list(workers.map(attempt, range(16)))
        assert outcomes.count("admitted") == 8
        assert outcomes.count("LOGIN_THROTTLED") == 8
    finally:
        second_store.close()
    reopened = Store(auth.store.directory)
    try:
        with pytest.raises(DevError) as throttled:
            Auth(reopened)._login_attempt(request("198.51.100.2"), "owner")
        assert throttled.value.code == "LOGIN_THROTTLED"
    finally:
        reopened.close()


def test_account_attempt_storage_has_a_hard_bound_and_expires(auth, monkeypatch):
    monkeypatch.setattr(auth_module, "LOGIN_ATTEMPT_STORAGE_LIMIT", 3)
    clock = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(auth_module, "time", SimpleNamespace(time=lambda: clock.value))
    for i in range(3):
        auth._login_attempt(request(f"192.0.2.{i + 1}"), f"unknown-{i}")
    with pytest.raises(DevError) as throttled:
        auth._login_attempt(request("198.51.100.1"), "unknown-overflow")
    assert throttled.value.code == "LOGIN_THROTTLED"
    assert auth.store.one("SELECT count(*) AS n FROM login_account_attempts")["n"] == 3
    clock.value = 1300.001
    auth._login_attempt(request("198.51.100.1"), "unknown-overflow")
    assert auth.store.one("SELECT count(*) AS n FROM login_account_attempts")["n"] == 1


def test_success_resets_only_the_successful_account_budget(auth):
    auth._login_attempt(request("198.51.100.1"), "another-user")
    async def exercise():
        for i in range(7):
            with pytest.raises(DevError):
                await auth.login(request(f"192.0.2.{i + 1}"), "owner", "wrong-password")
        value = await auth.login(request("192.0.2.20"), "owner", "correct-password")
        assert auth.session(request(cookie=value["cookie"]))["user_id"] == "owner"
        assert auth.store.one("SELECT count(*) AS n FROM login_account_attempts WHERE username_hash=?",
                              (digest("owner"),))["n"] == 0
        assert auth.store.one("SELECT count(*) AS n FROM login_account_attempts WHERE username_hash=?",
                              (digest("another-user"),))["n"] == 1
    asyncio.run(exercise())


def test_additive_migration_preserves_old_login_records_sessions_and_audit(auth):
    with auth.store.transaction():
        value = auth.new_session("owner")
        auth.store.db.execute("DROP TABLE login_account_attempts")
        auth.store.db.execute("INSERT INTO login_attempts VALUES ('192.0.2.1', 123)")
        auth.store.audit("panel:owner", "before-auth-migration")
    reopened = Store(auth.store.directory)
    try:
        assert Auth(reopened).session(request(cookie=value["cookie"]))["user_id"] == "owner"
        assert reopened.one("SELECT at FROM login_attempts WHERE ip='192.0.2.1'")["at"] == 123
        assert [row["name"] for row in reopened.all("PRAGMA table_info(login_attempts)")] == ["ip", "at"]
        assert reopened.one("SELECT count(*) AS n FROM login_account_attempts")["n"] == 0
        assert reopened.one("SELECT action FROM audit WHERE action='before-auth-migration'")
        assert reopened.decrypt(auth.store.encrypt("retained-key")) == "retained-key"
        assert reopened.all("PRAGMA foreign_key_check") == []
    finally:
        reopened.close()


@pytest.mark.parametrize("invalidated", ["expired", "epoch", "disabled", "missing-security"])
def test_sessions_check_expiry_account_state_and_revocation(auth, invalidated):
    with auth.store.transaction():
        value = auth.new_session("owner")
    assert auth.session(request(cookie=value["cookie"]))["user_id"] == "owner"
    if invalidated == "expired":
        auth.store.execute("UPDATE sessions SET expires=0")
    elif invalidated == "epoch":
        auth.store.execute("UPDATE iam_users SET epoch=epoch+1 WHERE user_id='owner'")
    elif invalidated == "disabled":
        auth.store.execute("UPDATE iam_users SET active=0 WHERE user_id='owner'")
    else:
        auth.store.execute("DELETE FROM session_security")
    with pytest.raises(DevError):
        auth.session(request(cookie=value["cookie"]))


def test_csrf_is_bound_to_the_session_and_rejects_cross_origin(auth):
    with auth.store.transaction():
        first, second = auth.new_session("owner"), auth.new_session("owner")
    assert first["csrf"] != second["csrf"]
    assert auth.session_write(request(cookie=first["cookie"], csrf=first["csrf"]))["user_id"] == "owner"
    with pytest.raises(DevError) as mismatch:
        auth.session_write(request(cookie=first["cookie"], csrf=second["csrf"]))
    assert mismatch.value.code == "CSRF_REJECTED"
    with pytest.raises(DevError) as cross_origin:
        auth.session_write(request(cookie=first["cookie"], csrf=first["csrf"], origin="https://elsewhere.invalid"))
    assert cross_origin.value.code == "ORIGIN_REJECTED"


def test_failed_login_audit_omits_raw_untrusted_username_and_password(auth):
    async def exercise():
        with pytest.raises(DevError):
            await auth.login(request(), "input-may-be-a-secret", "wrong-password")
    asyncio.run(exercise())
    row = auth.store.one("SELECT target,detail FROM audit WHERE action='auth.login' AND status='failed'")
    assert row["target"] == "username-sha256:" + digest("input-may-be-a-secret")
    assert "input-may-be-a-secret" not in str(row)
    assert "wrong-password" not in str(row)
