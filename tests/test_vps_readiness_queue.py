"""Isolated readiness coordinator tests. No SSH, system package manager or credentials."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager

import pytest

from hub.vps_readiness import VPSReadiness


class FakeStore:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE vps_connections (
          id TEXT PRIMARY KEY, space_id TEXT, owner_user_id TEXT,
          connection_revision INTEGER, enabled INTEGER)""")

    def execute(self, query, args=()):
        with self.db:
            return self.db.execute(query, args)

    def one(self, query, args=()):
        row = self.db.execute(query, args).fetchone()
        return dict(row) if row else None

    @contextmanager
    def transaction(self):
        with self.db:
            yield self.db

    async def run(self, func, *args):
        return func(*args)

    def target(self, value, space="space", owner="owner", revision=1, enabled=True):
        self.execute(
            "INSERT INTO vps_connections VALUES(?,?,?,?,?)",
            (value, space, owner, revision, enabled),
        )


def service(store, steps, *, allowed=None, **settings):
    async def authorize(target, action):
        assert target.authorized_user_id == "administrator"
        assert target.owner_user_id == "owner"
        steps.append((action + ":authorize", target.id))
        return allowed(target, action) if allowed else True

    async def probe(target):
        steps.append(("probe", target.id))
        return "missing" if len([key for key, _ in steps if key == "install"]) == 0 else "ready"

    async def provision(target):
        steps.append(("install", target.id))
        return "ok"

    settings.setdefault("clock", lambda: 100.0)
    queue = VPSReadiness(store, authorize=authorize, inspect=probe, provision=provision, **settings)
    queue._initialize()
    return queue


@pytest.mark.asyncio
async def test_backfill_is_space_scoped_enabled_and_manual_recheck_never_reinstalls():
    store, steps = FakeStore(), []
    store.target("target")
    store.target("unrelated", space="other")
    store.target("disabled", enabled=False)
    queue = service(store, steps)
    assert await queue.backfill("space", "scope-grant", authorized_user_id="administrator") == 1
    assert await queue.backfill("space", "scope-grant", authorized_user_id="administrator") == 0
    first = await queue.status("space", "target")
    same = await queue.enqueue(
        "space", "target", "scope-grant", authorized_user_id="administrator", expected_revision=1
    )
    assert first["id"] == same["id"]
    assert not await queue.status("other", "target")
    assert not await queue.status("space", "disabled")
    assert not await queue.status("other", "unrelated")
    assert not await queue.enqueue(
        "space", "target", "scope-grant", authorized_user_id="administrator", expected_revision=2
    )
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["state"] == "ready"
    assert steps == [
        (kind, first["id"])
        for kind in (
            "inspect:authorize",
            "probe",
            "install:authorize",
            "install",
            "inspect:authorize",
            "probe",
        )
    ]
    await queue.enqueue(
        "space",
        "target",
        "scope-grant",
        authorized_user_id="administrator",
        expected_revision=1,
        refresh=True,
    )
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["state"] == "ready"
    assert sum(key == "install" for key, _ in steps) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["owner", "space", "revision", "revoke", "disable"])
async def test_target_or_grant_change_between_inspection_and_install_is_blocked(mutation):
    store, steps = FakeStore(), []
    store.target("target")
    queue = service(
        store,
        steps,
        allowed=lambda target, action: not (mutation == "revoke" and action == "install"),
    )
    unmodified = queue.inspect

    async def race(target):
        row = {
            "owner": ("owner_user_id", "new-owner"),
            "space": ("space_id", "other"),
            "revision": ("connection_revision", 2),
            "disable": ("enabled", 0),
        }.get(mutation)
        if row:
            column, value = row
            store.execute(f"UPDATE vps_connections SET {column}=? WHERE id='target'", (value,))
        return await unmodified(target)

    queue.inspect = race
    job = await queue.enqueue(
        "space", "target", "grant", authorized_user_id="administrator", expected_revision=1
    )
    assert await queue.run_one()
    receipt = store.one("SELECT state,reason_code FROM vps_readiness_jobs WHERE id=?", (job["id"],))
    assert receipt == {"state": "failed", "reason_code": "TARGET_OR_GRANT_CHANGED"}
    assert ("install", job["id"]) not in steps


@pytest.mark.asyncio
async def test_interrupted_install_persists_checkpoint_and_restart_only_rechecks():
    store, steps = FakeStore(), []
    store.target("target")
    queue = service(store, steps)
    reached = asyncio.Event()

    async def interrupted(target):
        reached.set()
        await asyncio.Future()

    queue.provision = interrupted
    job = await queue.enqueue(
        "space", "target", "grant", authorized_user_id="administrator", expected_revision=1
    )
    worker = asyncio.create_task(queue.run_one())
    await asyncio.wait_for(reached.wait(), 1)
    assert (await queue.status("space", "target"))["state"] == "installing"
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert store.one("SELECT verify_only FROM vps_readiness_jobs WHERE id=?", (job["id"],)) == {
        "verify_only": 1
    }
    restarted = service(store, steps)
    assert await restarted.run_one()
    result = await restarted.status("space", "target")
    assert result["state"] == "failed"
    assert result["reason_code"] == "INSTALL_OUTCOME_UNCONFIRMED"
    await restarted.enqueue(
        "space",
        "target",
        "grant",
        authorized_user_id="administrator",
        expected_revision=1,
        refresh=True,
    )
    assert await restarted.run_one()
    assert sum(key == "install" for key, _ in steps) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,verified,reason",
    [
        ("install_not_started_denied", "missing", "VPS_PRIVILEGE_REQUIRED"),
        ("install_not_started_unsupported", "offline", "VPS_ENV_UNSUPPORTED"),
    ],
)
async def test_definite_no_install_receipt_allows_manual_recheck_only(outcome, verified, reason):
    store, steps = FakeStore(), []
    store.target("target")
    queue = service(store, steps)
    probes = iter(("missing", verified, "missing", "ready"))
    provisions = iter((outcome, "ok"))

    async def inspect(_target):
        return next(probes)

    async def provision(target):
        steps.append(("install", target.id))
        return next(provisions)

    queue.inspect = inspect
    queue.provision = provision
    kwargs = dict(authorized_user_id="administrator", expected_revision=1)
    job = await queue.enqueue("space", "target", "grant", **kwargs)
    assert await queue.run_one()
    after_first = await queue.status("space", "target")
    assert after_first["state"] == "failed"
    assert after_first["reason_code"] == reason
    assert not await queue.run_one()
    await queue.enqueue("space", "target", "grant", refresh=True, **kwargs)
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["state"] == "ready"
    assert [entry for entry in steps if entry[0] == "install"] == [("install", job["id"])] * 2


@pytest.mark.asyncio
async def test_offline_probe_is_retried_with_bounded_backoff_and_never_installs():
    now, steps, store = [100.0], [], FakeStore()
    store.target("target")
    queue = service(store, steps, clock=lambda: now[0], retry_delays=(3, 10))

    async def offline(_target):
        return "offline"

    queue.inspect = offline
    await queue.enqueue(
        "space", "target", "grant", authorized_user_id="administrator", expected_revision=1
    )
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["state"] == "queued"
    assert not await queue.run_one()
    now[0] += 3
    assert await queue.run_one()
    assert not await queue.run_one()
    now[0] += 10
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["state"] == "failed"
    assert not [key for key, _ in steps if key == "install"]


@pytest.mark.asyncio
async def test_two_workers_deduplicate_same_target_while_other_target_can_progress():
    store, steps = FakeStore(), []
    store.target("first")
    store.target("second")
    queue = service(store, steps)
    hit, unblock = asyncio.Event(), asyncio.Event()
    previous = queue.provision

    async def block_first(target):
        if target.vps_id == "first":
            hit.set()
            await unblock.wait()
        return await previous(target)

    queue.provision = block_first
    job = await queue.enqueue(
        "space", "first", "grant", authorized_user_id="administrator", expected_revision=1
    )
    task = asyncio.create_task(queue.run_one())
    await asyncio.wait_for(hit.wait(), 1)
    dupe = await queue.enqueue(
        "space",
        "first",
        "grant",
        authorized_user_id="administrator",
        expected_revision=1,
        refresh=True,
    )
    assert dupe["id"] == job["id"]
    assert dupe["state"] == "installing"
    assert not await queue.run_one()
    await queue.enqueue(
        "space", "second", "grant", authorized_user_id="administrator", expected_revision=1
    )
    assert await queue.run_one()
    assert (await queue.status("space", "second"))["state"] == "ready"
    unblock.set()
    assert await asyncio.wait_for(task, 1)
    assert sum(key == "install" and value == job["id"] for key, value in steps) == 1
