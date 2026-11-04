"""Successful login refunds its own reservation, never somebody else's budget."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

import hub.auth as auth_module
from hub.auth import Auth
from hub.store import Store
from shared.crypto import digest
from shared.util import DevError
from tests.test_bug_report_auth_20261020 import auth as auth, request


def counts(auth):
    return tuple(auth.store.one(f"SELECT count(*) AS n FROM {table}")["n"]
                 for table in ("login_attempts", "login_account_attempts"))


def test_many_normal_successes_do_not_exhaust_shared_ip(auth):
    async def scenario():
        for _ in range(24):
            session = await auth.login(request(), "owner", "correct-password")
            assert auth.session(request(cookie=session["cookie"]))["user_id"] == "owner"
            assert counts(auth) == (0, 0)
    asyncio.run(scenario())


def test_success_failure_interleaving_keeps_all_failed_budgets(auth):
    async def scenario():
        for number in range(7):
            with pytest.raises(DevError) as exc:
                await auth.login(request(), "owner" if number % 2 else "unknown", "wrong-password")
            assert exc.value.code == "LOGIN_FAILED"
            await auth.login(request(), "owner", "correct-password")
            assert counts(auth) == (number + 1, number + 1)
        with pytest.raises(DevError):
            await auth.login(request(), "unknown-final", "wrong-password")
        with pytest.raises(DevError) as exc:
            await auth.login(request(), "owner", "correct-password")
        assert exc.value.code == "LOGIN_THROTTLED"
        assert counts(auth) == (8, 8)
    asyncio.run(scenario())


def test_concurrent_success_cannot_refund_other_inflight_or_failed_login(auth, monkeypatch):
    original = auth_module.password_verify
    barrier = threading.Barrier(4)
    def verify(password, encoded):
        value = original(password, encoded)
        barrier.wait(timeout=10)
        return value
    monkeypatch.setattr(auth_module, "password_verify", verify)
    async def scenario():
        for batch in range(3):
            results = await asyncio.gather(*(
                auth.login(request(), "owner", password)
                for password in ("correct-password", "wrong-password", "correct-password", "wrong-password")
            ), return_exceptions=True)
            assert sum(isinstance(value, dict) for value in results) == 2
            assert sum(isinstance(value, DevError) and value.code == "LOGIN_FAILED" for value in results) == 2
            assert counts(auth) == ((batch + 1) * 2, (batch + 1) * 2)
    asyncio.run(scenario())


def test_same_timestamp_reservations_across_stores_refund_only_exact_rows(auth, monkeypatch):
    monkeypatch.setattr(auth_module, "time", SimpleNamespace(time=lambda: 1000.0))
    second_store = Store(auth.store.directory)
    second = Auth(second_store)
    try:
        # OAuth has historically shared this table without account reservations.
        auth.store.execute("INSERT INTO login_attempts VALUES (?,?)", ("192.0.2.1", 1000.0))
        ip, success = auth._login_attempt(request(), "owner")
        second._login_attempt(request(), "owner")
        second._login_attempt(request(), "another-user")
        auth._login_session(success, ip, "owner")
        assert counts(auth) == (3, 2)
        assert auth.store.one("SELECT count(*) AS n FROM login_account_attempts WHERE username_hash=?",
                              (digest("owner"),))["n"] == 1
    finally:
        second_store.close()


def test_failed_session_creation_does_not_refund_reservation(auth, monkeypatch):
    ip, user = auth._login_attempt(request(), "owner")
    def fail(*args):
        raise RuntimeError("session write failed")
    monkeypatch.setattr(auth, "new_session", fail)
    with pytest.raises(RuntimeError, match="session write failed"):
        auth._login_session(user, ip, "owner")
    assert counts(auth) == (1, 1)
