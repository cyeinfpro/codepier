"""Real snapshot/cancellation races use temporary stores and explicit barriers."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
import threading
import time
from zoneinfo import ZoneInfo

import pytest
from starlette.requests import Request

from hub.token_cache_schema import revision
from hub.mcp_usage import RETENTION_SECONDS
from shared.token_estimate import usage
from shared.util import DevError
from tests.test_audit_api import api  # noqa: F401


def _principal(app, token):
    return app.state.auth.bearer(Request({
        "type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())],
    }))


def _clock():
    return datetime(2026, 10, 10, 12, tzinfo=ZoneInfo("UTC")).timestamp()


def _metric(n):
    return dict(state="available", estimated_tokens=n, low=n, high=n,
                characters=n, utf8_bytes=n, source_truncated=False, truncated=False)


def _seed(app, principal, started, *, record=True, response=1000):
    store = app.state.store
    project = store.one("SELECT * FROM projects WHERE id='project'")
    cursor = store.execute(
        "INSERT INTO mcp_activity(project,root,device,grant_id,actor,window_key,tool,started,service_ms,status,transition,meaningful) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (project["id"], project["root"], project["device_id"], principal.grant_id, principal.actor,
         "synthetic-window", "read", started, 100, "complete", "serial", 1))
    if record:
        app.state.runtime.integrations.usage_metrics.record(cursor.lastrowid, usage(_metric(100), _metric(response)))
    return cursor.lastrowid


def _pause_aggregation(monkeypatch, module, store, *, workers=1):
    started, release = threading.Event(), threading.Event()
    calls, mutex = [], threading.Lock()
    original = module.context_usage

    def paused(*args, **kwargs):
        assert not store.lock.owned
        assert threading.current_thread().name.startswith("hub-token")
        with mutex:
            calls.append(1)
            if len(calls) >= workers:
                started.set()
        assert release.wait(20), "test did not release the aggregation barrier"
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "context_usage", paused)
    return started, release, calls


def test_aggregation_releases_database_lock(api, monkeypatch):
    import hub.token_dashboard as module
    app, _client, token = api
    p = _principal(app, token)
    _seed(app, p, _clock() - 60)
    original = module.context_usage

    def unlocked(*args, **kwargs):
        assert not app.state.store.lock.owned
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "context_usage", unlocked)
    result = module.token_dashboard(app.state.runtime, p, ZoneInfo("UTC"), now=_clock())
    assert result["summary"]["wire_attempts"] == 1


def test_unrelated_heartbeats_and_audit_writes_do_not_invalidate_exact_cache(api, monkeypatch):
    import hub.token_dashboard as module
    app, _client, token = api
    runtime, store, p = app.state.runtime, app.state.store, _principal(app, token)
    _seed(app, p, _clock() - 60)
    calls, original = [], module.context_usage

    def measured(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "context_usage", measured)
    first = module.token_dashboard(runtime, p, ZoneInfo("UTC"), now=_clock())
    initial_revision = revision(store)
    store.execute("UPDATE devices SET last_seen=? WHERE id='device'", (time.time(),))
    store.audit(p.actor, "synthetic.heartbeat", status="ok")
    second = module.token_dashboard(runtime, p, ZoneInfo("UTC"), now=_clock())
    assert first["summary"] == second["summary"]
    assert revision(store) == initial_revision
    assert len(calls) == 1


def test_expensive_http_statistics_do_not_block_health_or_light_overview(api, monkeypatch):
    import hub.token_dashboard as module
    app, client, token = api
    _seed(app, _principal(app, token), time.time() - 60)
    started, release, calls = _pause_aggregation(monkeypatch, module, app.state.store)
    with ThreadPoolExecutor(max_workers=2) as requests:
        pending = requests.submit(client.get, "/api/token-usage")
        try:
            assert started.wait(5)
            health = requests.submit(client.get, "/healthz").result(timeout=5)
            assert health.status_code == 200
            overview = requests.submit(client.get, "/api/overview?include_token_usage=false").result(timeout=5)
            assert overview.status_code == 200
            assert overview.json()["token_usage"] is None
            assert not pending.done()
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result.status_code == 200
    assert result.json()["freshness"]["policy"] == "bounded_snapshot"
    assert len(calls) == 1


def test_combined_overview_rechecks_short_retention_after_final_worker_wait(api, monkeypatch):
    import hub.token_dashboard as module
    app, client, token = api
    store, real_time = app.state.store, time.time
    identifier = _seed(app, _principal(app, token), real_time() - 60)
    store.execute("UPDATE mcp_usage SET created=? WHERE activity_id=?",
                  (real_time() - RETENTION_SECONDS + 2, identifier))
    run, combined_phases = store.run, []

    async def delayed(function, *args, **kwargs):
        if function.__name__ == "overview_snapshot":
            combined_phases.append(1)
            # The token read already validated its two-second retention TTL.
            # Model time passing while the final overview DB phase was queued.
            monkeypatch.setattr(module.time, "time", lambda: real_time() + 3)
        return await run(function, *args, **kwargs)

    monkeypatch.setattr(store, "run", delayed)
    response = client.get("/api/overview")
    assert combined_phases == [1]
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STATISTICS_CHANGED"


def test_same_scope_singleflight_survives_one_reader_cancellation(api, monkeypatch):
    import hub.token_dashboard as module
    app, client, token = api
    runtime, p = app.state.runtime, _principal(app, token)
    _seed(app, p, _clock() - 60)
    started, release, calls = _pause_aggregation(monkeypatch, module, app.state.store)

    async def scenario():
        arrived = asyncio.Event()
        entered = 0
        original = module._DashboardService.get

        async def observed(self, *args):
            nonlocal entered
            entered += 1
            if entered == 12:
                arrived.set()
            return await original(self, *args)

        monkeypatch.setattr(module._DashboardService, "get", observed)
        pending = [asyncio.create_task(module.token_dashboard_async(runtime, p, ZoneInfo("UTC"), now=_clock()))
                   for _ in range(12)]
        try:
            await asyncio.wait_for(arrived.wait(), 5)
            assert await asyncio.to_thread(started.wait, 5)
            pending[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending[0]
            assert len(runtime._token_dashboard_service.inflight) == 1
            assert not pending[1].done()
        finally:
            release.set()
        results = await asyncio.gather(*pending[1:])
        assert len(calls) == 1
        assert runtime._token_dashboard_service.computed == 1
        assert runtime._token_dashboard_service.coalesced == 11
        assert runtime._token_dashboard_service.inflight == {}
        results[0]["summary"]["wire_attempts"] = 999
        assert results[1]["summary"]["wire_attempts"] == 1

    client.portal.call(scenario)


def test_distinct_scope_backlog_is_bounded_before_loading_large_snapshots(api, monkeypatch):
    import hub.token_dashboard as module
    app, client, token = api
    runtime, p = app.state.runtime, _principal(app, token)
    _seed(app, p, _clock() - 60)
    started, release, _ = _pause_aggregation(monkeypatch, module, app.state.store, workers=module.COMPUTE_WORKERS)
    snapshots, original_snapshot = [], module._read_snapshot

    def observed_snapshot(*args):
        snapshots.append(1)
        return original_snapshot(*args)

    monkeypatch.setattr(module, "_read_snapshot", observed_snapshot)

    async def scenario():
        arrived, entered = asyncio.Event(), 0
        original = module._DashboardService.get

        async def observed(self, *args):
            nonlocal entered
            entered += 1
            if entered == module.MAX_INFLIGHT:
                arrived.set()
            return await original(self, *args)

        monkeypatch.setattr(module._DashboardService, "get", observed)
        pending = [asyncio.create_task(module.token_dashboard_async(runtime, p, ZoneInfo("UTC"),
                                                                   now=_clock(), session=f"scope-{index}"))
                   for index in range(module.MAX_INFLIGHT)]
        try:
            await asyncio.wait_for(arrived.wait(), 5)
            assert await asyncio.to_thread(started.wait, 5)
            assert len(runtime._token_dashboard_service.inflight) == module.MAX_INFLIGHT
            assert len(snapshots) == module.COMPUTE_WORKERS
            with pytest.raises(DevError) as error:
                await module.token_dashboard_async(runtime, p, ZoneInfo("UTC"), now=_clock(), session="overflow")
            assert error.value.code == "STATISTICS_BUSY" and error.value.status == 503
            assert len(snapshots) == module.COMPUTE_WORKERS
        finally:
            release.set()
            await asyncio.gather(*pending)
        assert runtime._token_dashboard_service.inflight == {}
        assert len(runtime._token_dashboard_cache) <= module.CACHE_ENTRIES

    client.portal.call(scenario)


def test_live_writes_are_coalesced_but_late_metrics_refresh_after_bounded_age(api):
    import hub.token_dashboard as module
    app, client, token = api
    runtime, p = app.state.runtime, _principal(app, token)
    identifier = _seed(app, p, _clock() - 60, record=False)

    async def read():
        return await module.token_dashboard_async(runtime, p, ZoneInfo("UTC"), now=_clock())

    first = client.portal.call(read)
    assert first["summary"]["input"]["estimated_tokens"] is None
    runtime.integrations.usage_metrics.record(identifier, usage(_metric(100), _metric(1000)))
    _seed(app, p, _clock() - 30)
    cached = client.portal.call(read)
    assert cached["summary"] == first["summary"]
    assert cached["freshness"]["as_of"] == first["freshness"]["as_of"]
    assert runtime._token_dashboard_service.computed == 1
    # Expire the deadline, not a database revision: high-rate writes must not
    # extend the snapshot's lifetime or starve a refresh indefinitely.
    with app.state.store.lock:
        for key, entry in list(runtime._token_dashboard_cache.items()):
            runtime._token_dashboard_cache[key] = replace(entry, expires=0, wall_expires=0)
    refreshed = client.portal.call(read)
    assert refreshed["summary"]["wire_attempts"] == 2
    assert refreshed["summary"]["input"]["estimated_tokens"] > 0
    assert runtime._token_dashboard_service.computed == 2
    retained = repr(runtime._token_dashboard_cache)
    assert p.token_hash and p.token_hash not in retained
    assert "/tmp/fixture" not in retained
    assert "usage_json" not in retained


def test_revocation_during_compute_is_checked_before_return(api, monkeypatch):
    import hub.token_dashboard as module
    app, client, token = api
    runtime, store, p = app.state.runtime, app.state.store, _principal(app, token)
    _seed(app, p, _clock() - 60)
    started, release, _ = _pause_aggregation(monkeypatch, module, store)

    async def scenario():
        pending = asyncio.create_task(module.token_dashboard_async(runtime, p, ZoneInfo("UTC"), now=_clock()))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await store.run(store.execute, "UPDATE grants SET revoked=1 WHERE id=?", (p.grant_id,))
        finally:
            release.set()
        with pytest.raises(DevError) as error:
            await pending
        assert error.value.code == "INVALID_TOKEN"
        with pytest.raises(DevError):
            await module.token_dashboard_async(runtime, p, ZoneInfo("UTC"), now=_clock())

    client.portal.call(scenario)


def test_activity_reassignment_and_grant_label_bypass_metric_refresh_window(api):
    import hub.token_dashboard as module
    app, client, token = api
    runtime, store, p = app.state.runtime, app.state.store, _principal(app, token)
    identifier = _seed(app, p, _clock() - 60)

    async def read():
        return await module.token_dashboard_async(runtime, p, ZoneInfo("UTC"), now=_clock())

    first = client.portal.call(read)
    store.execute("UPDATE grants SET label='new synthetic label' WHERE id=?", (p.grant_id,))
    renamed = client.portal.call(read)
    assert renamed["authority_key"] != first["authority_key"]
    assert renamed["options"]["connections"][0]["label"] == "new synthetic label"
    store.execute("UPDATE mcp_activity SET grant_id='different-grant' WHERE id=?", (identifier,))
    hidden = client.portal.call(read)
    assert hidden["summary"]["wire_attempts"] == 0
    assert hidden["options"]["connections"] == []


def test_revision_triggers_rollback_with_the_changed_metrics(api):
    app, _client, token = api
    store, p = app.state.store, _principal(app, token)
    identifier = _seed(app, p, _clock() - 60)
    before = revision(store)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with store.transaction():
            store.db.execute("UPDATE mcp_usage SET created=0 WHERE activity_id=?", (identifier,))
            store.db.execute("UPDATE mcp_activity SET actor='other-actor' WHERE id=?", (identifier,))
            assert revision(store)[0] > before[0]
            assert revision(store)[1] > before[1]
            raise RuntimeError("synthetic rollback")
    assert revision(store) == before
    assert store.one("SELECT actor FROM mcp_activity WHERE id=?", (identifier,))["actor"] == p.actor


def test_computation_failure_releases_singleflight_and_does_not_cache_failure(api, monkeypatch):
    import hub.token_dashboard as module
    app, client, token = api
    runtime, p = app.state.runtime, _principal(app, token)
    _seed(app, p, _clock() - 60)
    original = module.context_usage

    def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic computation failure")

    async def read():
        return await module.token_dashboard_async(runtime, p, ZoneInfo("UTC"), now=_clock())

    monkeypatch.setattr(module, "context_usage", broken)
    with pytest.raises(RuntimeError, match="synthetic computation failure"):
        client.portal.call(read)
    assert runtime._token_dashboard_service.inflight == {}
    assert runtime._token_dashboard_cache == {}
    monkeypatch.setattr(module, "context_usage", original)
    assert client.portal.call(read)["summary"]["wire_attempts"] == 1


def test_shutdown_cancellation_drains_computation_before_releasing_service(api, monkeypatch):
    import hub.token_dashboard as module
    app, client, token = api
    runtime, store, p = app.state.runtime, app.state.store, _principal(app, token)
    _seed(app, p, _clock() - 60)
    started, release, _ = _pause_aggregation(monkeypatch, module, store)

    async def scenario():
        pending = asyncio.create_task(module.token_dashboard_async(runtime, p, ZoneInfo("UTC"), now=_clock()))
        assert await asyncio.to_thread(started.wait, 5)
        service = runtime._token_dashboard_service
        closing = asyncio.create_task(module.aclose_token_dashboard(runtime))
        try:
            # Queue a real DB phase so shutdown has entered without sleeping.
            await store.run(lambda: None)
            assert service.closed
            closing.cancel()
            await store.run(lambda: None)
            assert not closing.done()
            with pytest.raises(DevError) as error:
                await module.token_dashboard_async(runtime, p, ZoneInfo("UTC"), now=_clock())
            assert error.value.code == "STATISTICS_UNAVAILABLE"
        finally:
            release.set()
            with pytest.raises(DevError) as interrupted:
                await pending
            assert interrupted.value.code == "STATISTICS_UNAVAILABLE"
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert getattr(runtime, "_token_dashboard_service", None) is None
        assert service.inflight == {}
        assert store.one("SELECT 1 AS value")["value"] == 1

    client.portal.call(scenario)
