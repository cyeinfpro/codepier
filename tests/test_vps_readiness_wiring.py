"""Readiness HTTP and IAM wiring against an isolated Store and fake native executor."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from hub.app import create_app
from shared.crypto import password_hash
from tests.legacy_iam_fixture import seed_owner
from tests.test_ssh_terminal_api import BrowserIdentity


ORIGIN = "https://testserver"
LOGIN = "readiness-fixture-login"
FIXED_PIN = "SHA256:" + "A" * 43


class SyntheticExecutor:
    def __init__(self):
        self.inspect_calls = []
        self.install_calls = []
        self.installed = set()

    async def inspect(self, target):
        self.inspect_calls.append(target)
        return "ready" if target.vps_id in self.installed else "missing"

    async def provision(self, target):
        self.install_calls.append(target)
        self.installed.add(target.vps_id)
        return "ok"


def prepare(tmp_path, monkeypatch, executor):
    monkeypatch.setenv("HUB_PUBLIC_URL", ORIGIN)
    monkeypatch.delenv("CODEPIER_VPS_READINESS_NATIVE", raising=False)
    app = create_app(str(tmp_path / "hub"), readiness_executor_factory=lambda _: executor)
    store = app.state.store
    hashed = password_hash(LOGIN)
    for user in ("owner", "admin", "member"):
        seed_owner(store, user, user)
        store.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, user))
    with store.transaction() as db:
        for space in ("primary", "other"):
            db.execute(
                "INSERT INTO spaces(id,label,kind,created) VALUES(?,?,'team',?)",
                (space, space, time.time()),
            )
            for user, role in (("owner", "owner"), ("admin", "admin"), ("member", "member")):
                db.execute(
                    "INSERT INTO memberships(space_id,user_id,level) VALUES(?,?,?)",
                    (space, user, role),
                )
    return app


def identity(client, user="owner", space="primary"):
    response = client.post(
        "/api/login", json={"username": user, "password": LOGIN}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    client.cookies.clear()
    return BrowserIdentity(client, response.cookies["rd_session"], response.json()["csrf"], space)


def add(client, label, *, enabled=True, pin=True):
    payload = {
        "name": label,
        "host": label + ".invalid",
        "username": "fixture",
        "password": "readiness-synthetic-password",
        "project_ids": [],
        "enabled": enabled,
    }
    if pin:
        payload.update(host_key_policy="confirm-change", host_key_fingerprint=FIXED_PIN)
    response = client.post("/api/vps", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def wait_job(identity, identifier, *, state="ready"):
    endpoint = f"/api/vps/{identifier}/readiness"
    for _ in range(160):
        response = identity.get(endpoint)
        assert response.status_code == 200, response.text
        job = response.json()["job"]
        if job and job["state"] == state:
            return job
        time.sleep(0.025)
    raise AssertionError(f"job did not settle in {state}: {job}")


def test_scope_activation_backfill_save_and_authority_renewal(tmp_path, monkeypatch):
    native = SyntheticExecutor()
    app = prepare(tmp_path, monkeypatch, native)
    store = app.state.store
    with TestClient(app, base_url=ORIGIN) as client:
        owner = identity(client)
        next_admin = identity(client, "admin")
        member = identity(client, "member")
        other = BrowserIdentity(client, owner.cookie, owner.csrf, "other")
        # Simulate inventory saved before native readiness was available.
        readiness = app.state.runtime.vps_readiness
        readiness.started = False
        try:
            previous = add(owner, "saved")
            disabled = add(owner, "disabled", enabled=False)
            old_trust = add(owner, "legacy", pin=False)
            unrelated = add(other, "unrelated")
        finally:
            readiness.started = True
        assert owner.get("/api/vps").json()["readiness_backfill_available"]
        assert not owner.get("/api/vps").json()["readiness_backfill_enabled"]
        assert not member.get("/api/vps").json()["readiness_backfill_available"]
        current = owner.get(f"/api/vps/{previous['id']}").json()
        assert current["readiness_available"] and current["readiness"] is None
        assert not disabled["readiness_available"]
        endpoint = f"/api/vps/{previous['id']}/readiness"
        assert member.get(endpoint).status_code == 403
        assert other.get(endpoint).status_code == 404
        assert member.post("/api/vps/readiness/backfill").status_code == 403
        assert (
            owner.post("/api/vps/readiness/backfill", headers={"X-RD-CSRF": "wrong"}).status_code
            == 403
        )
        activation = owner.post("/api/vps/readiness/backfill")
        assert activation.status_code == 200, activation.text
        assert activation.json() == {"enabled": True, "queued_count": 2}
        assert owner.get("/api/vps").json()["readiness_backfill_enabled"]
        first = wait_job(owner, previous["id"])
        assert owner.get(f"/api/vps/{previous['id']}").json()["readiness"]["id"] == first["id"]
        failed = wait_job(owner, old_trust["id"], state="failed")
        assert failed["reason_code"] == "HUB_SSH_TRUST_MIGRATION_REQUIRED"
        assert other.get(f"/api/vps/{unrelated['id']}/readiness").json()["job"] is None
        newly_saved = add(owner, "new-saved")
        assert newly_saved["readiness"]["state"] in ("queued", "running", "installing", "ready")
        wait_job(owner, newly_saved["id"])

        store.execute(
            "UPDATE memberships SET level='member' WHERE user_id='owner' AND space_id='primary'"
        )
        assert owner.post(endpoint).status_code == 403
        recheck = next_admin.post(endpoint)
        assert recheck.status_code == 200, recheck.text
        assert recheck.json()["job"]["id"] == first["id"]
        wait_job(next_admin, previous["id"])
        installs = [call for call in native.install_calls if call.vps_id == previous["id"]]
        assert len(installs) == 1 and installs[0].authorized_user_id == "owner"
        assert any(
            call.authorized_user_id == "admin" and call.id == first["id"]
            for call in native.inspect_calls
        )
        assert all(call.space_id == "primary" for call in native.inspect_calls)


def test_readiness_is_unavailable_without_explicit_native_executor(tmp_path, monkeypatch):
    app = prepare(tmp_path, monkeypatch, None)
    with TestClient(app, base_url=ORIGIN) as client:
        owner = identity(client)
        value = add(owner, "saved-without-executor")
        assert value["readiness_available"] is False
        assert value["readiness"] is None
        route = f"/api/vps/{value['id']}/readiness"
        assert owner.get(route).json() == {"available": False, "job": None}
        assert owner.post(route).status_code == 503
        assert owner.post("/api/vps/readiness/backfill").status_code == 503
        assert (
            app.state.store.one("SELECT name FROM sqlite_master WHERE name='vps_readiness_jobs'")
            is None
        )


def test_admin_save_auto_queues_own_vps_before_space_activation(tmp_path, monkeypatch):
    native = SyntheticExecutor()
    app = prepare(tmp_path, monkeypatch, native)
    with TestClient(app, base_url=ORIGIN) as client:
        owner = identity(client)
        assert not owner.get("/api/vps").json()["readiness_backfill_enabled"]
        saved = add(owner, "individual-only")
        first = wait_job(owner, saved["id"])
        assert saved["readiness"]["id"] == first["id"]
        assert not owner.get("/api/vps").json()["readiness_backfill_enabled"]
        changed = owner.put(
            f"/api/vps/{saved['id']}",
            json={
                "name": saved["name"],
                "host": saved["host"],
                "port": saved["port"],
                "username": saved["username"],
                "host_key_policy": "confirm-change",
                "host_key_fingerprint": FIXED_PIN,
                "password": "new-synthetic-password",
                "project_ids": [],
                "expected_version": saved["version"],
            },
        )
        assert changed.status_code == 200, changed.text
        next_job = wait_job(owner, saved["id"])
        assert next_job["id"] != first["id"]
        assert next_job["connection_revision"] == first["connection_revision"] + 1
        assert len(native.install_calls) == 1
