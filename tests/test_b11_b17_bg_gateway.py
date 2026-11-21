"""Regression tests for B11 (remote gateway lock-free handshake & concurrency) and B17 (collaboration idle polling & connection_evidence throttle/zero-decrypt)."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from hub.auth import Auth
from hub.collaboration.common import STATUS_EVENT, timestamp
from hub.collaboration.config import CollaborationConfig
from hub.collaboration.schema import migrate as migrate_collaboration
from hub.gateway.remote import BackendError, RemotePool, Session
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from shared.contracts import TOOLS
from shared.mcp_protocol import MODERN
from shared.util import DevError
from tests.legacy_iam_fixture import seed_owner
from tests.test_mcp_gateway import Backend, public_dns


def _reset_dedup(evidence):
    """Test-only: start from an empty observation de-duplication window."""
    with evidence._lru_lock:
        evidence._observed_lru.clear()


CONFIG = {
    "endpoint": "https://mcp.example/mcp",
    "protocol": "auto",
    "networks": "[]",
    "allow_http": 0,
}


def _seed_hub(tmp_path):
    store = Store(tmp_path / "hub")
    with store.transaction():
        migrate_collaboration(store.db)
    seed_owner(store, "owner", "owner")
    store.execute(
        "INSERT INTO devices(id,name,secret,created) VALUES('node','Fixture node',?,?)",
        (store.encrypt("disposable-device-secret"), time.time()),
    )
    store.execute(
        """INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created)
        VALUES('project','fixture','fixture','node',?,'write',1,?)""",
        (str(tmp_path / "project"), time.time()),
    )
    runtime = Runtime(
        store,
        collaboration_config=CollaborationConfig(
            enabled=True,
            events_enabled=True,
            collector_enabled=True,
        ),
    )
    auth = Auth(store)
    viewer = Principal("panel:owner", "owner", {"read", "write", "execute"}, ["*"], admin=True)
    grant = auth.issue_grant(viewer, "Fixture connection", ["read"], ["project"], 30)
    row = store.one("SELECT * FROM grants WHERE id=?", (grant["grant_id"],))
    principal = runtime.grant_principal(row)
    return store, runtime, viewer, principal, grant


@pytest.mark.asyncio
async def test_collaboration_idle_poll_and_connection_evidence_throttled(tmp_path, monkeypatch):
    store, runtime, viewer, principal, grant = _seed_hub(tmp_path)
    try:
        service = runtime.collaboration
        clock = [time.time()]
        service.clock = lambda: clock[0]

        run_calls = []
        run_read_calls = []
        orig_run = store.run
        orig_run_read = store.run_read

        async def tracked_run(fn, /, *args, **kwargs):
            run_calls.append(getattr(fn, "__name__", repr(fn)))
            return await orig_run(fn, *args, **kwargs)

        async def tracked_run_read(fn, /, *args, **kwargs):
            run_read_calls.append(getattr(fn, "__name__", repr(fn)))
            return await orig_run_read(fn, *args, **kwargs)

        monkeypatch.setattr(store, "run", tracked_run)
        monkeypatch.setattr(store, "run_read", tracked_run_read)

        with store.lock:
            before_changes = store.db.total_changes

        # Start all collaboration background loops in idle state and step 15s of idle time.
        await service.loops.start()
        try:
            await asyncio.sleep(0.15)
            for _ in range(3):
                clock[0] += 5
                await service.events.tick(wake=service.loops._event_wake)
                await service.loops.reconcile()
                await service.monitor.tick()

            with store.lock:
                after_idle_changes = store.db.total_changes

            assert len(run_calls) == 0
            assert len(run_read_calls) >= 3
            assert after_idle_changes == before_changes

            # Idle backoff must sit in [15, 30] seconds across loops.
            assert 15 <= service.events.next_dispatch_delay(2) <= 30
            assert 15 <= service.loops._idle_delay_for("reconcile", 5, 5) <= 30
            assert 15 <= service.monitor.next_collect_delay(20) <= 30

            # Immediate wakeup via asyncio.Event (wake.set()) on a real collaboration write.
            work_settled = asyncio.Event()
            orig_work = service.events._run_tick_work

            def spy_work(*args, **kwargs):
                try:
                    return orig_work(*args, **kwargs)
                finally:
                    if store._event_loop is not None:
                        store._event_loop.call_soon_threadsafe(work_settled.set)

            monkeypatch.setattr(service.events, "_run_tick_work", spy_work)
            with store.transaction():
                _, room = service.scope(
                    viewer,
                    {"project": "project", "environment_id": "production"},
                    create=True,
                )
                # Insert an expired active subscription so consolidated discover_deliveries
                # runs once and settles without external callback network I/O.
                store.execute(
                    """INSERT INTO mcp_event_subscriptions
                    (id,room_id,principal,grant_id,name,arguments,secret,expires_at,verified_until,key_digest,created,updated)
                    VALUES ('sub-wake',?,'{}',?,?,'{}','unused',?,?, 'kd', ?,?)""",
                    (
                        room["id"],
                        grant["grant_id"],
                        STATUS_EVENT,
                        clock[0] - 1,
                        clock[0] + 60,
                        clock[0],
                        clock[0],
                    ),
                )
                service.emit(
                    room,
                    STATUS_EVENT,
                    "job-wake-1",
                    1,
                    {
                        "component": "analysis",
                        "status": "queued",
                        "reason_code": "queued",
                        "observed_at": timestamp(clock[0]),
                        "recovery_state": "pending",
                    },
                )

            await asyncio.wait_for(work_settled.wait(), timeout=2.0)
            # Consolidated event tick uses 1 writer call (not 7 serial calls).
            assert len(run_calls) == 1
            assert (
                store.one("SELECT state FROM mcp_event_subscriptions WHERE id='sub-wake'")["state"]
                == "expired"
            )
        finally:
            await service.loops.stop()

        # Part B: connection_evidence 60s LRU throttle + zero Fernet re-decrypt/SELECTs.
        evidence = runtime.diagnostics.connection
        _reset_dedup(evidence)
        project = runtime.project("project", principal)

        decrypt_calls = []
        orig_decrypt = store.decrypt

        def tracked_decrypt(value):
            decrypt_calls.append(value)
            return orig_decrypt(value)

        monkeypatch.setattr(store, "decrypt", tracked_decrypt)

        evidence_inserts = []
        orig_execute = store.execute

        def tracked_execute(sql, args=()):
            if "INSERT INTO mcp_connection_evidence" in sql:
                evidence_inserts.append(sql)
            return orig_execute(sql, args)

        monkeypatch.setattr(store, "execute", tracked_execute)

        gp_calls = []
        op_calls = []
        proj_calls = []
        orig_gp = runtime.grant_principal
        orig_op = runtime.operation_row
        orig_proj = runtime.project

        def tracked_gp(*a, **kw):
            gp_calls.append(1)
            return orig_gp(*a, **kw)

        def tracked_op(*a, **kw):
            op_calls.append(1)
            return orig_op(*a, **kw)

        def tracked_proj(*a, **kw):
            proj_calls.append(1)
            return orig_proj(*a, **kw)

        monkeypatch.setattr(runtime, "grant_principal", tracked_gp)
        monkeypatch.setattr(runtime, "operation_row", tracked_op)
        monkeypatch.setattr(runtime, "project", tracked_proj)

        read_args = TOOLS["read"].model.model_validate(
            {
                "project": "fixture",
                "path": "README.md",
                "idempotency_key": "b17-evidence-0001",
            }
        ).model_dump()
        request_payload = {
            "tool": "read",
            "args": read_args,
            "project": {"id": project["id"], "root": project["root"]},
        }

        base_wall = time.time()
        for i in range(20):
            op_row = {
                "id": f"op-read-{i}",
                "tool": "read",
                "grant_id": principal.grant_id,
                "project_id": project["id"],
                "device_id": project["device_id"],
                "state": "running",
                "args_summary": json.dumps({"path": "README.md"}),
                "payload": None,
            }
            evidence.completed(
                op_row,
                {"ok": True, "data": {"path": "README.md", "content": "hello"}},
                state="succeeded",
                completed_at=base_wall + i * 0.4,
                principal=principal,
                project=project,
                tool="read",
                operation=op_row,
                payload=request_payload,
            )
            evidence.returned(principal, "read", read_args, {"operation_id": op_row["id"]})

        assert len(decrypt_calls) == 0
        assert len(gp_calls) == 0
        assert len(op_calls) == 0
        assert len(proj_calls) == 0
        # 1 write for "readonly" + 1 write for "call" across 20 reads within 10s.
        assert len(evidence_inserts) == 2

        # Backward compatibility: single-arg calls work and a cleared cache writes again.
        evidence.completed({"id": "noop", "tool": "read", "grant_id": ""})
        _reset_dedup(evidence)
        evidence.completed(
            {
                "id": "op-read-after-reset",
                "tool": "read",
                "grant_id": principal.grant_id,
                "project_id": project["id"],
                "device_id": project["device_id"],
                "state": "succeeded",
                "args_summary": "{}",
            },
            {"ok": True, "data": {"path": "README.md", "content": "hello"}},
            state="succeeded",
            completed_at=base_wall + 9.0,
            principal=principal,
            project=project,
            tool="read",
            payload=request_payload,
        )
        assert len(evidence_inserts) == 3
    finally:
        store.close()


@pytest.mark.asyncio
async def test_connection_evidence_throttles_different_tools_and_resets(tmp_path, monkeypatch):
    store, runtime, _, principal, _ = _seed_hub(tmp_path)
    try:
        evidence = runtime.diagnostics.connection
        _reset_dedup(evidence)
        inserts = []
        orig_execute = store.execute

        def tracked_execute(sql, args=()):
            if "INSERT INTO mcp_connection_evidence" in sql:
                inserts.append(args)
            return orig_execute(sql, args)

        monkeypatch.setattr(store, "execute", tracked_execute)

        for _ in range(5):
            evidence.returned(principal, "read", {}, {"ok": True})
        for _ in range(5):
            evidence.returned(principal, "task_query", {}, {"ok": True})
        evidence.observe(principal, "catalog", catalog_sha256="a" * 64)
        evidence.observe(principal, "catalog", catalog_sha256="a" * 64)
        evidence.observe(principal, "catalog", catalog_sha256="b" * 64)

        assert len(inserts) == 4  # read, task_query, catalog(a), catalog(b)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_remote_mcp_initialize_releases_condition_and_supports_concurrency():
    # Part 1: Slow 2s initialize() releases self.condition so close() and
    # concurrent waiters never deadlock on self.condition.
    init_entered = asyncio.Event()
    allow_init = asyncio.Event()

    async def slow_handle(request):
        body = json.loads(request.content)
        method = body["method"]
        rid = body.get("id")
        if method == "server/discover":
            init_entered.set()
            try:
                await asyncio.wait_for(allow_init.wait(), timeout=2.0)
            except TimeoutError:
                pass
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "resultType": "complete",
                        "supportedVersions": [MODERN],
                        "capabilities": {"tools": {}},
                    },
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": "ok"}],
                },
            },
            request=request,
        )

    session = Session(
        CONFIG,
        "backend-secret",
        resolver=public_dns,
        transport=httpx.MockTransport(slow_handle),
    )

    first = asyncio.create_task(session.call("echo", {"value": "first"}, lambda: None))
    await asyncio.wait_for(init_entered.wait(), timeout=1.0)

    # self.condition must NOT be held during the network handshake.
    assert session.initializing is True
    assert session.condition.locked() is False

    second = asyncio.create_task(session.call("echo", {"value": "second"}, lambda: None))
    await asyncio.sleep(0.02)
    assert len(session.waiters) == 2

    # A queued caller cannot clear the initializer's ownership. close()
    # rejects new admission immediately but drains the real handshake.
    closing = asyncio.create_task(session.close(drain_seconds=1.0))
    with pytest.raises(DevError) as closed_exc:
        await asyncio.wait_for(second, timeout=0.5)
    assert closed_exc.value.code == "GATEWAY_CLOSED"
    assert session.initializing and not closing.done()

    allow_init.set()
    with pytest.raises((DevError, BackendError, asyncio.CancelledError)):
        await asyncio.wait_for(first, timeout=1.0)
    await asyncio.wait_for(closing, timeout=0.5)
    assert session.client is None

    # Part 1b: On initialize() failure, initializing resets and wakes next waiter.
    attempts = 0

    async def flaky_init():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BackendError("GATEWAY_BACKEND_UNAVAILABLE", "init failed", 502)
        flaky_session.version = MODERN
        flaky_session.stateless = True

    flaky_session = Session(CONFIG, "backend-secret", resolver=public_dns)
    flaky_session.initialize = flaky_init
    flaky_session.rpc = lambda method, params=None, **kw: asyncio.sleep(0, result={"ok": True})
    try:
        t_fail = asyncio.create_task(flaky_session.call("echo", {"value": "1"}, lambda: None))
        t_next = asyncio.create_task(flaky_session.call("echo", {"value": "2"}, lambda: None))
        with pytest.raises(BackendError):
            await asyncio.wait_for(t_fail, timeout=1.0)
        assert flaky_session.initializing is False
        assert await asyncio.wait_for(t_next, timeout=1.0) == {"ok": True}
    finally:
        await flaky_session.close()

    # Part 2: Configured concurrency cannot override a legacy session.
    in_flight = 0
    max_in_flight = 0
    legacy_backend = Backend(modern=False)

    async def concurrent_legacy_handle(request):
        nonlocal in_flight, max_in_flight
        body = json.loads(request.content)
        if body["method"] == "tools/call":
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0)
                return await legacy_backend.handle(request)
            finally:
                in_flight -= 1
        return await legacy_backend.handle(request)

    with pytest.raises(ValueError):
        Session({**CONFIG, "concurrency": 0}, "secret")
    with pytest.raises(ValueError):
        Session({**CONFIG, "concurrency": 33}, "secret")

    # Without explicit concurrency, legacy/stateful still defaults safely to 1.
    default_legacy = Session(
        {**CONFIG, "protocol": "legacy"},
        "secret",
        resolver=public_dns,
        transport=httpx.MockTransport(legacy_backend.handle),
    )
    try:
        await default_legacy.call("echo", {"value": "single"}, lambda: None)
        assert default_legacy.version != MODERN
        assert default_legacy._effective_concurrency() == 1
    finally:
        await default_legacy.close()

    # With an explicit cap of 4, these requests still dispatch serially.
    pool = RemotePool(
        resolver=public_dns,
        transport=httpx.MockTransport(concurrent_legacy_handle),
    )
    connector_cfg = {**CONFIG, "protocol": "legacy", "concurrency": 4}
    key = ("call", "space", "user", "grant", "binding", 1, "acc", 1, "hash", "conn", 1)
    pooled_session = await pool.get(key, connector_cfg, "secret")
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                pooled_session.call("echo", {"value": "a"}, lambda: None),
                pooled_session.call("echo", {"value": "b"}, lambda: None),
                pooled_session.call("echo", {"value": "c"}, lambda: None),
            ),
            timeout=2.0,
        )
        assert len(results) == 3
        assert max_in_flight == 1
        assert pooled_session.stats["peak_active"] == 1
        pooled_session.version = MODERN
        assert pooled_session._effective_concurrency() == 1  # stateful
        pooled_session.stateless = True
        assert pooled_session._effective_concurrency() == 4
    finally:
        await pool.release(key)
        await pool.close()
