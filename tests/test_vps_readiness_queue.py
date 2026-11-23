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
        return "ready" if ("install", target.id) in steps else "missing"

    async def provision(target, *, before_install):
        assert await before_install()
        steps.append(("install", target.id))
        return "ok"

    settings.setdefault("clock", lambda: 100.0)
    queue = VPSReadiness(store, authorize=authorize, inspect=probe, provision=provision, **settings)
    queue._initialize()
    return queue


@pytest.mark.asyncio
async def test_backfill_is_read_only_and_install_requires_its_current_positive_result():
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
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 0
    assert await queue.run_one()
    observed = await queue.status("space", "target")
    assert observed["state"] == "missing"
    assert observed["reason_code"] == "TMUX_MISSING"
    assert observed["intent"] == "scan"
    assert observed["install_eligible"] is True
    assert steps == [("inspect:authorize", first["id"]), ("probe", first["id"])]
    assert await queue.install_missing("other", "install", authorized_user_id="administrator") == 0
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 0
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["state"] == "ready"
    assert steps == [
        (kind, first["id"])
        for kind in (
            "inspect:authorize",
            "probe",
            "inspect:authorize",
            "probe",
            "install:authorize",
            "install",
            "inspect:authorize",
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

    job = await queue.enqueue(
        "space", "target", "grant", authorized_user_id="administrator", expected_revision=1
    )
    assert await queue.run_one()
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
    queue.inspect = race
    assert await queue.run_one()
    receipt = store.one("SELECT state,reason_code FROM vps_readiness_jobs WHERE id=?", (job["id"],))
    assert receipt == {"state": "failed", "reason_code": "TARGET_OR_GRANT_CHANGED"}
    assert ("install", job["id"]) not in steps


@pytest.mark.asyncio
async def test_interrupted_install_persists_checkpoint_and_restart_only_rechecks():
    store, steps = FakeStore(), []
    store.target("target")
    queue = service(store, steps, retry_delays=())
    reached = asyncio.Event()

    async def interrupted(target, *, before_install):
        assert await before_install()
        reached.set()
        await asyncio.Future()

    queue.provision = interrupted
    job = await queue.enqueue(
        "space", "target", "grant", authorized_user_id="administrator", expected_revision=1
    )
    assert await queue.run_one()
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
    worker = asyncio.create_task(queue.run_one())
    await asyncio.wait_for(reached.wait(), 1)
    assert (await queue.status("space", "target"))["state"] == "installing"
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert store.one("SELECT verify_only FROM vps_readiness_jobs WHERE id=?", (job["id"],)) == {
        "verify_only": 1
    }
    restarted = service(store, steps, retry_delays=())
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
    assert (
        await restarted.install_missing("space", "install", authorized_user_id="administrator") == 0
    )
    assert sum(key == "install" for key, _ in steps) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,reason",
    [
        ("install_not_started_denied", "VPS_PRIVILEGE_REQUIRED"),
        ("install_not_started_unsupported", "VPS_ENV_UNSUPPORTED"),
    ],
)
async def test_definite_no_install_receipt_allows_manual_recheck_only(outcome, reason):
    store, steps = FakeStore(), []
    store.target("target")
    queue = service(store, steps)
    probes = iter(("missing", "missing", "missing", "missing"))
    provisions = iter((outcome, "ok"))

    async def inspect(_target):
        return next(probes)

    async def provision(target, *, before_install):
        assert await before_install()
        steps.append(("install", target.id))
        return next(provisions)

    queue.inspect = inspect
    queue.provision = provision
    kwargs = dict(authorized_user_id="administrator", expected_revision=1)
    job = await queue.enqueue("space", "target", "grant", **kwargs)
    assert await queue.run_one()
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
    assert await queue.run_one()
    after_first = await queue.status("space", "target")
    assert after_first["state"] == "failed"
    assert after_first["reason_code"] == reason
    assert not await queue.run_one()
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 0
    await queue.enqueue("space", "target", "grant", refresh=True, **kwargs)
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["state"] == "missing"
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
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

    async def block_first(target, *, before_install):
        async def before_and_hold():
            assert await before_install()
            if target.vps_id == "first":
                hit.set()
                await unblock.wait()
            return True

        return await previous(target, before_install=before_and_hold)

    queue.provision = block_first
    job = await queue.enqueue(
        "space", "first", "grant", authorized_user_id="administrator", expected_revision=1
    )
    assert await queue.run_one()
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
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
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 0
    assert not await queue.run_one()
    await queue.enqueue(
        "space", "second", "grant", authorized_user_id="administrator", expected_revision=1
    )
    assert await queue.run_one()
    assert (await queue.status("space", "second"))["state"] == "missing"
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
    assert await queue.run_one()
    assert (await queue.status("space", "second"))["state"] == "ready"
    unblock.set()
    assert await asyncio.wait_for(task, 1)
    assert sum(key == "install" and value == job["id"] for key, value in steps) == 1


@pytest.mark.asyncio
async def test_fleet_install_skips_stale_and_non_missing_and_continues_after_failure():
    store, steps = FakeStore(), []
    for name in ("bad", "good", "changed", "has-tmux", "unknown"):
        store.target(name)
    queue = service(store, steps)
    previous = queue.inspect

    async def inspect(target):
        if target.vps_id == "has-tmux":
            return "ready"
        if target.vps_id == "unknown":
            return "trust_missing"
        return await previous(target)

    async def provision(target, *, before_install):
        assert await before_install()
        if target.vps_id == "bad":
            raise RuntimeError("no receipt")
        steps.append(("install", target.id))
        return "ok"

    queue.inspect = inspect
    queue.provision = provision
    assert await queue.backfill("space", "scan", authorized_user_id="administrator") == 5
    for _ in range(5):
        assert await queue.run_one()
    store.execute("UPDATE vps_connections SET connection_revision=2 WHERE id='changed'")
    assert (
        await queue.install_missing(
            "space",
            "install",
            authorized_user_id="administrator",
            vps_id="good",
            expected_revision=2,
        )
        == 0
    )
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 2
    assert await queue.run_one()
    assert await queue.run_one()
    assert not await queue.run_one()
    assert (await queue.status("space", "bad"))["reason_code"] == "INSTALL_OUTCOME_UNCONFIRMED"
    assert (await queue.status("space", "good"))["state"] == "ready"
    assert (await queue.status("space", "has-tmux"))["state"] == "ready"
    assert (await queue.status("space", "unknown"))[
        "reason_code"
    ] == "HUB_SSH_TRUST_MIGRATION_REQUIRED"
    assert await queue.status("space", "changed") is None
    assert (await queue.status("space", "bad"))["state"] == "queued"
    # The other four targets may be rescanned while the bad target waits for
    # its scheduled read-only retry. Do not reset or replace its checkpoint.
    assert (
        await queue.backfill("space", "scan", authorized_user_id="administrator", refresh=True) == 4
    )


@pytest.mark.asyncio
async def test_upgrade_defaults_legacy_queued_job_to_scan_and_never_replays_old_install():
    from hub.vps_readiness import _SCHEMA

    store, steps = FakeStore(), []
    for name in ("legacy", "running-install"):
        store.target(name)
    store.execute(
        "\n".join(
            line
            for line in _SCHEMA.splitlines()
            if "intent TEXT" not in line and "host_verification" not in line
        )
    )
    for name, state, held in (("legacy", "queued", 0), ("running-install", "installing", 1)):
        store.execute(
            """INSERT INTO vps_readiness_jobs
                (id,space_id,vps_id,owner_user_id,authorized_user_id,connection_revision,grant_id,
                 state,verify_only,next_attempt,updated)
                VALUES(?,?,?,'owner','administrator',1,'old-grant',?,?,0,0)
            """,
            (name, "space", name, state, held),
        )
    queue = service(store, steps)
    assert await queue.run_one()
    assert await queue.run_one()
    assert (await queue.status("space", "legacy"))["state"] == "missing"
    old = await queue.status("space", "running-install")
    assert old["intent"] == "scan"
    assert old["host_verification"] == "required"
    assert old["reason_code"] == "INSTALL_OUTCOME_UNCONFIRMED"
    assert not any(kind == "install" for kind, _ in steps)


@pytest.mark.asyncio
async def test_expired_and_future_scan_evidence_needs_a_fresh_probe():
    store, steps, now = FakeStore(), [], [100.0]
    store.target("target")
    queue = service(store, steps, clock=lambda: now[0])
    assert await queue.backfill("space", "scan", authorized_user_id="administrator") == 1
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["install_eligible"] is True
    now[0] += 1_801
    stale = await queue.status("space", "target")
    assert stale["state"] == "missing" and stale["install_eligible"] is False
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 0
    now[0] = 90
    assert (await queue.status("space", "target"))["install_eligible"] is False
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 0
    assert (
        await queue.backfill("space", "scan", authorized_user_id="administrator", refresh=True) == 1
    )
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["install_eligible"] is True
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["install_eligible"] is False


@pytest.mark.asyncio
async def test_verified_install_does_not_prevent_reinstall_after_later_removal():
    store, steps = FakeStore(), []
    store.target("target")
    queue = service(store, steps)
    opts = dict(authorized_user_id="administrator", expected_revision=1, refresh=True)
    first = await queue.enqueue("space", "target", "scan", **opts)
    assert await queue.run_one()
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["state"] == "ready"
    assert steps.count(("install", first["id"])) == 1

    # The remote host uninstalls tmux later without editing the saved connection.
    steps.clear()
    await queue.enqueue("space", "target", "scan", **opts)
    assert await queue.run_one()
    result = await queue.status("space", "target")
    assert result["state"] == "missing", result
    assert result["install_eligible"]
    assert await queue.install_missing("space", "install", authorized_user_id="administrator") == 1
    assert await queue.run_one()
    assert steps.count(("install", first["id"])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,code",
    [
        ("host_key_unknown", "HOST_KEY_UNKNOWN"),
        ("host_key_changed", "HOST_KEY_CHANGED"),
        ("host_key_revoked", "HOST_KEY_REVOKED"),
        ("ssh_auth_failed", "SSH_AUTH_FAILED"),
    ],
)
async def test_probe_keeps_specific_connection_failure_ineligible_for_install(outcome, code):
    db, steps = FakeStore(), []
    db.target("target")
    queue = service(db, steps)

    async def inspect(_target):
        return outcome

    queue.inspect = inspect
    await queue.enqueue(
        "space", "target", "scan", authorized_user_id="administrator", expected_revision=1
    )
    assert await queue.run_one()
    job = await queue.status("space", "target")
    assert job["state"] == "failed"
    assert job["reason_code"] == code
    assert not job["install_eligible"]


@pytest.mark.asyncio
async def test_upgrade_clears_only_definitively_ready_historical_fences():
    store, steps = FakeStore(), []
    for name in ("verified", "uncertain", "installing"):
        store.target(name)
    queue = service(store, steps)
    await queue.backfill("space", "scope", authorized_user_id="administrator")
    store.execute(
        """UPDATE vps_readiness_jobs SET verify_only=1,
                 state=CASE vps_id WHEN 'verified' THEN 'ready' WHEN 'installing' THEN 'installing' ELSE 'failed' END,
                 reason_code=CASE WHEN vps_id='uncertain' THEN 'INSTALL_OUTCOME_UNCONFIRMED' ELSE NULL END"""
    )
    queue._initialize()
    for name, expected in (("verified", 0), ("uncertain", 1), ("installing", 1)):
        row = store.one("SELECT verify_only FROM vps_readiness_jobs WHERE vps_id=?", (name,))
        assert row["verify_only"] == expected


@pytest.mark.asyncio
async def test_install_matches_scan_mode_without_overwriting_an_active_job():
    db, steps = FakeStore(), []
    db.target("target")
    queue = service(db, steps)
    admin = dict(authorized_user_id="administrator")
    assert await queue.backfill("space", "verified", **admin) == 1
    db.execute("UPDATE vps_readiness_jobs SET attempts=1")
    assert (
        await queue.backfill("space", "skip", refresh=True, host_verification="skip", **admin) == 0
    )
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["host_verification"] == "required"
    assert (
        await queue.install_missing("space", "skip-install", host_verification="skip", **admin) == 0
    )
    assert (
        await queue.backfill("space", "skip", refresh=True, host_verification="skip", **admin) == 1
    )
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["host_verification"] == "skip"
    assert await queue.install_missing("space", "verified-install", **admin) == 0
    assert (
        await queue.install_missing("space", "skip-install", host_verification="skip", **admin) == 1
    )
    assert await queue.run_one()
    assert [kind for kind, _ in steps].count("install") == 1


@pytest.mark.asyncio
async def test_mode_change_cannot_clear_prior_unconfirmed_install():
    db, steps = FakeStore(), []
    db.target("target")
    queue = service(db, steps)
    admin = dict(authorized_user_id="administrator")
    await queue.backfill("space", "verified", **admin)
    db.execute(
        """UPDATE vps_readiness_jobs SET state='failed',verify_only=1,
            reason_code='INSTALL_OUTCOME_UNCONFIRMED'"""
    )
    assert (
        await queue.backfill("space", "skip", refresh=True, host_verification="skip", **admin) == 0
    )
    old = await queue.enqueue(
        "space",
        "target",
        "skip",
        expected_revision=1,
        refresh=True,
        host_verification="skip",
        **admin,
    )
    assert old["host_verification"] == "required"
    assert old["reason_code"] == "INSTALL_OUTCOME_UNCONFIRMED"
    assert not await queue.run_one()
    assert await queue.backfill("space", "verified", refresh=True, **admin) == 1
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["reason_code"] == "INSTALL_OUTCOME_UNCONFIRMED"
    assert not any(kind == "install" for kind, _ in steps)


@pytest.mark.asyncio
async def test_explicit_fleet_scan_rebinds_only_own_old_unsent_read_only_job():
    db, steps = FakeStore(), []
    for target in ("legacy", "fenced", "retried", "foreign-admin", "queued-install"):
        db.target(target)
    queue = service(db, steps)
    admin = dict(authorized_user_id="administrator")
    assert await queue.backfill("space", "verified", **admin) == 5
    db.execute("UPDATE vps_readiness_jobs SET verify_only=1 WHERE vps_id='fenced'")
    db.execute("UPDATE vps_readiness_jobs SET attempts=1 WHERE vps_id='retried'")
    db.execute(
        "UPDATE vps_readiness_jobs SET authorized_user_id='second' WHERE vps_id='foreign-admin'"
    )
    db.execute("UPDATE vps_readiness_jobs SET intent='install' WHERE vps_id='queued-install'")
    assert (
        await queue.backfill("space", "skip", refresh=True, host_verification="skip", **admin) == 1
    )
    assert (await queue.status("space", "legacy"))["host_verification"] == "skip"
    for target in ("fenced", "retried", "foreign-admin", "queued-install"):
        assert (await queue.status("space", target))["host_verification"] == "required"


@pytest.mark.asyncio
async def test_read_only_ssh_interruption_recovers_without_install():
    now, steps, store = [100.0], [], FakeStore()
    store.target("target")
    queue = service(store, steps, clock=lambda: now[0], retry_delays=(3,))
    probes = iter(("probe_interrupted", "ready"))

    async def inspect(_target):
        return next(probes)

    queue.inspect = inspect
    await queue.backfill("space", "scan", authorized_user_id="administrator")
    assert await queue.run_one()
    first = await queue.status("space", "target")
    assert (first["state"], first["reason_code"], first["verification_only"]) == (
        "queued",
        "VPS_PROBE_INTERRUPTED",
        False,
    )
    assert not await queue.run_one()
    now[0] += 3
    assert await queue.run_one()
    assert (await queue.status("space", "target"))["state"] == "ready"
    assert all(kind != "install" for kind, _ in steps)


@pytest.mark.asyncio
async def test_retry_before_package_dispatch_and_verify_after_lost_receipt():
    now, steps, store = [100.0], [], FakeStore()
    store.target("target")
    queue = service(store, steps, clock=lambda: now[0], retry_delays=(3,))
    admin = {"authorized_user_id": "administrator"}
    probes = iter(("missing", "missing", "missing", "missing", "ready"))
    calls = []

    async def inspect(_target):
        return next(probes)

    async def provision(_target, *, before_install):
        calls.append("preflight")
        if len(calls) == 1:
            return "probe_interrupted"
        assert await before_install()
        calls.append("dispatched")
        return "uncertain"

    queue.inspect, queue.provision = inspect, provision
    await queue.backfill("space", "scan", **admin)
    assert await queue.run_one()
    assert await queue.install_missing("space", "install", **admin) == 1
    assert await queue.run_one()
    early = await queue.status("space", "target")
    assert early["state"] == "queued" and not early["verification_only"]
    now[0] += 3
    assert await queue.run_one()
    late = await queue.status("space", "target")
    assert late["state"] == "queued" and late["verification_only"]
    assert late["reason_code"] == "INSTALL_OUTCOME_UNCONFIRMED"
    assert await queue.install_missing("space", "install", **admin) == 0
    assert not await queue.run_one()
    now[0] += 3
    assert await queue.run_one()
    result = await queue.status("space", "target")
    assert result["state"] == "ready" and not result["verification_only"]
    assert calls == ["preflight", "preflight", "dispatched"]


@pytest.mark.asyncio
async def test_reported_package_failure_is_distinct_and_stays_fenced():
    steps, store = [], FakeStore()
    store.target("target")
    queue = service(store, steps)
    admin = {"authorized_user_id": "administrator"}

    async def provision(_target, *, before_install):
        assert await before_install()
        return "install_failed"

    queue.provision = provision
    await queue.backfill("space", "scan", **admin)
    assert await queue.run_one()
    assert await queue.install_missing("space", "install", **admin) == 1
    assert await queue.run_one()
    status = await queue.status("space", "target")
    assert status["state"] == "failed" and status["verification_only"]
    assert status["reason_code"] == "TMUX_INSTALL_FAILED"
    assert await queue.install_missing("space", "install", **admin) == 0
