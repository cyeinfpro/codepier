"""Readiness HTTP and IAM wiring against an isolated Store and fake native executor."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from fastapi.testclient import TestClient

from hub import iam
from hub.app import create_app
from hub.vps_readiness import ReadinessTarget
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

    async def provision(self, target, *, before_install):
        assert await before_install()
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


def test_manual_scan_and_install_are_separate_and_space_scoped(tmp_path, monkeypatch):
    native = SyntheticExecutor()
    app = prepare(tmp_path, monkeypatch, native)
    with TestClient(app, base_url=ORIGIN) as client:
        owner = identity(client)
        member = identity(client, "member")
        other = BrowserIdentity(client, owner.cookie, owner.csrf, "other")
        missing = add(owner, "missing")
        ready = add(owner, "ready")
        disabled = add(owner, "disabled", enabled=False)
        old_trust = add(owner, "legacy", pin=False)
        unrelated = add(other, "unrelated")
        native.installed.add(ready["id"])
        assert missing["readiness"] is None
        assert disabled["readiness_available"] is False
        owner_inventory = owner.get("/api/vps").json()
        assert owner_inventory["readiness_manual_actions"]
        assert owner_inventory["readiness_connection_contract"] == "vps_ssh_v2"
        assert owner_inventory["readiness_host_identity_verification"] == "disabled"
        assert not member.get("/api/vps").json()["readiness_manual_actions"]

        scan = "/api/vps/readiness/scan"
        install = "/api/vps/readiness/install"
        for route in (scan, install):
            assert member.post(route).status_code == 403
            assert owner.post(route, headers={"X-RD-CSRF": "wrong"}).status_code == 403
        assert owner.post(install).json() == {"queued_count": 0}
        # Unified vps_ssh_v2: backfill, scan, and install all use skip semantics regardless of legacy pin.
        assert (
            owner.post("/api/vps/readiness/backfill", json={"host_verification": "skip"}).json()[
                "queued_count"
            ]
            == 3
        )
        queued = wait_job(owner, missing["id"], state="missing")
        assert queued["reason_code"] == "TMUX_MISSING"
        assert queued["host_verification"] == "skip"
        assert wait_job(owner, ready["id"])["intent"] == "scan"
        assert wait_job(owner, old_trust["id"], state="missing")["host_verification"] == "skip"
        assert other.get(f"/api/vps/{unrelated['id']}/readiness").json()["job"] is None
        assert native.install_calls == []

        assert owner.post(install).json()["queued_count"] == 2
        ended = wait_job(owner, missing["id"])
        assert wait_job(owner, old_trust["id"])["host_verification"] == "skip"
        assert ended["id"] == queued["id"]
        assert ended["intent"] == "install"
        assert owner.post(install).json()["queued_count"] == 0
        assert {call.vps_id for call in native.install_calls} == {missing["id"], old_trust["id"]}
        assert len(native.install_calls) == 2
        assert all(call.host_verification == "skip" for call in native.install_calls)
        assert all(call.space_id == "primary" for call in native.inspect_calls)
        assert owner.get(f"/api/vps/{missing['id']}").json()["legacy_host_trust"]["host_key_fingerprint"] == FIXED_PIN


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


def test_connection_change_invalidates_scan_before_fleet_install(tmp_path, monkeypatch):
    native = SyntheticExecutor()
    app = prepare(tmp_path, monkeypatch, native)
    with TestClient(app, base_url=ORIGIN) as client:
        owner = identity(client)
        saved = add(owner, "individual-only")
        path = f"/api/vps/{saved['id']}/readiness"
        assert saved["readiness"] is None
        assert owner.post(path).status_code == 200
        first = wait_job(owner, saved["id"], state="missing")
        assert first["intent"] == "scan"
        assert first["host_verification"] == "skip"
        assert native.install_calls == []
        changed = owner.put(
            f"/api/vps/{saved['id']}",
            json={
                "name": saved["name"],
                "host": saved["host"],
                "port": saved["port"],
                "username": saved["username"],
                "host_key_policy": "confirm-change",
                "host_key_fingerprint": FIXED_PIN,
                "password": "updated-fixture-password",
                "project_ids": [],
                "expected_version": saved["version"],
            },
        )
        assert changed.status_code == 200, changed.text
        assert owner.get(path).json()["job"] is None
        assert owner.post("/api/vps/readiness/install").json()["queued_count"] == 0
        assert native.install_calls == []
        assert owner.post(path).status_code == 200
        next_job = wait_job(owner, saved["id"], state="missing")
        assert next_job["id"] != first["id"]
        assert next_job["connection_revision"] == first["connection_revision"] + 1
        assert owner.post("/api/vps/readiness/install").json()["queued_count"] == 1
        wait_job(owner, saved["id"])
        assert len(native.install_calls) == 1


@pytest.mark.parametrize("when", ["queued", "installing"])
def test_other_admin_scan_does_not_revoke_explicit_install(tmp_path, monkeypatch, when):
    native = SyntheticExecutor()
    app = prepare(tmp_path, monkeypatch, native)
    wiring = app.state.runtime.vps_readiness
    entered, resume = threading.Event(), threading.Event()
    original = native.provision

    async def manual_start():
        await wiring.store.run(wiring.queue._initialize)

    async def gated_provision(target, *, before_install):
        async def blocked_checkpoint():
            assert await before_install()
            if when == "installing":
                entered.set()
                assert await asyncio.to_thread(resume.wait, 10)
            return await wiring.authorize(target, "install")

        if not await wiring.authorize(target, "install"):
            return "denied"
        return await original(target, before_install=blocked_checkpoint)

    monkeypatch.setattr(wiring.queue, "start", manual_start)
    monkeypatch.setattr(wiring.queue, "provision", gated_provision)
    with TestClient(app, base_url=ORIGIN) as client:
        owner = identity(client)
        admin = identity(client, "admin")
        target = add(owner, "concurrent")
        endpoint = f"/api/vps/{target['id']}/readiness"
        assert owner.post("/api/vps/readiness/scan").status_code == 200
        assert client.portal.call(wiring.queue.run_one)
        assert owner.get(endpoint).json()["job"]["state"] == "missing"
        assert owner.post("/api/vps/readiness/install").json() == {"queued_count": 1}
        worker = (
            client.portal.start_task_soon(wiring.queue.run_one) if when == "installing" else None
        )
        try:
            if worker:
                assert entered.wait(10)
                assert owner.get(endpoint).json()["job"]["state"] == "installing"
            # Both a second admin's scan and duplicate install click may occur
            # while the first admin's explicitly authorized install is pending.
            assert admin.post("/api/vps/readiness/scan").json()["queued_count"] == 0
            assert admin.post("/api/vps/readiness/install").json() == {"queued_count": 0}
        finally:
            resume.set()
        result = worker.result(timeout=10) if worker else client.portal.call(wiring.queue.run_one)
        assert result
        assert owner.get(endpoint).json()["job"]["state"] == "ready"
        assert len(native.install_calls) == 1


def test_legacy_scan_grant_is_migrated_without_install_permission(tmp_path, monkeypatch):
    native = SyntheticExecutor()
    app = prepare(tmp_path, monkeypatch, native)
    wiring = app.state.runtime.vps_readiness
    store = app.state.store
    store.execute(
        """CREATE TABLE vps_readiness_grants (
        id TEXT PRIMARY KEY,space_id TEXT NOT NULL,authorized_user_id TEXT NOT NULL,user_epoch INTEGER NOT NULL,
        identity_id TEXT,vps_id TEXT,active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),created REAL NOT NULL)"""
    )
    store.execute(
        """CREATE UNIQUE INDEX vps_readiness_one_space_grant
        ON vps_readiness_grants(space_id) WHERE vps_id IS NULL AND active=1"""
    )
    epoch = iam.user_security(store, "owner")["epoch"]
    store.execute(
        """INSERT INTO vps_readiness_grants(id,space_id,authorized_user_id,user_epoch,created)
        VALUES('legacy','primary','owner',?,?)""",
        (epoch, time.time()),
    )

    async def manual_start():
        await wiring.store.run(wiring.queue._initialize)

    monkeypatch.setattr(wiring.queue, "start", manual_start)
    with TestClient(app, base_url=ORIGIN) as client:
        owner = identity(client)
        saved = add(owner, "legacy-grant")
        row = store.one("SELECT owner_user_id FROM vps_connections WHERE id=?", (saved["id"],))
        job = ReadinessTarget(
            id="synthetic",
            space_id="primary",
            vps_id=saved["id"],
            owner_user_id=row["owner_user_id"],
            authorized_user_id="owner",
            connection_revision=saved["connection_revision"],
            grant_id="legacy",
        )
        assert (
            store.one("SELECT intent FROM vps_readiness_grants WHERE id='legacy'")["intent"]
            == "scan"
        )
        assert client.portal.call(wiring.authorize, job, "inspect")
        assert not client.portal.call(wiring.authorize, job, "install")
