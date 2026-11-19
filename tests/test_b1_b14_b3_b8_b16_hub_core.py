"""Focused unit tests for Hub core performance & security optimizations (B1, B3, B8, B12, B14, B16, B17)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from hub import room_scope
from hub.api.devices import device_rows
from hub.diagnostics import Diagnostics
from hub.grant_expiry import update as update_grant_expiry
from hub.runtime import CANCELLABLE_READ_TOOLS, Connection, Principal, Runtime
from hub.scheduler_settings import view as scheduler_settings_view
from hub.store import Store
from shared import crypto as shared_crypto
from shared.agent_lifecycle import public_management
from shared.config import RuntimeConfig
from shared.crypto import ASYNC_OFFLOAD_BYTES, SecureChannel, pack_channel, unpack_channel
from shared.util import DevError
from tests.legacy_iam_fixture import seed_owner


def _seed_hub(tmp_path):
    store = Store(tmp_path / "hub")
    seed_owner(store, "owner", "admin")
    store.execute(
        "INSERT INTO users(id,username,password_hash,created) VALUES ('peer','peer','unused',1)"
    )
    store.execute(
        "INSERT INTO devices(id,name,secret,created,space_id,owner_user_id) VALUES ('dev','home',?,?, 'legacy', 'owner')",
        (store.encrypt(shared_crypto.token()), time.time()),
    )
    store.execute(
        "INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created,space_id,owner_user_id) "
        "VALUES ('proj','ProjectAlpha','projectalpha','dev','/tmp/fixture','','write',1,?,'legacy','owner')",
        (time.time(),),
    )
    runtime = Runtime(store)
    runtime.wait_seconds = 0
    return store, runtime


def _grant_row(store, grant_id: str, user_id: str, scopes: list[str], *, space_id: str = "legacy"):
    store.execute(
        "INSERT INTO grants(id,user_id,label,scopes,projects,revoked,created,space_id) VALUES (?,?,?,?,?,0,?,?)",
        (
            grant_id,
            user_id,
            f"label-{grant_id}",
            json.dumps(scopes),
            json.dumps(["*"]),
            time.time(),
            space_id,
        ),
    )


def test_heartbeat_without_security_change_keeps_security_epoch_and_revision_stable(tmp_path):
    store, runtime = _seed_hub(tmp_path)
    try:
        conn = SimpleNamespace(
            last_seen=time.time(),
            _persisted_last_seen=time.time(),
            scheduler_protocol=1,
            unusable=False,
            device_secret=None,
            runtime_snapshot=None,
        )
        runtime.connections["dev"] = conn
        runtime.connection_authorized = lambda device_id, c: c is conn

        info = {
            "build": {"version": "1.0"},
            "device_actions": ["agent_restart"],
            "management": public_management(
                {
                    "managed": True,
                    "service": True,
                    "service_kind": "systemd",
                    "status": "running",
                    "last_error": "",
                    "reason": "",
                    "installed_version": "1.0",
                }
            ),
            "scheduler_error": "",
            "scheduler_protocol": 1,
        }
        store.execute(
            "UPDATE devices SET info=? WHERE id='dev'",
            (json.dumps(info),),
            security_write=False,
        )

        # Capture baseline after seeding.
        epoch_before = store.security_epoch()
        rev_before = store.session_revision
        statements: list[str] = []
        with store.lock:
            store.db.set_trace_callback(statements.append)

        # Steady-state heartbeats varying runtime scheduler telemetry must not write `devices`
        # or change security_epoch / session_revision.
        for active_exec in (1, 3, 2):
            payload = {
                "type": "heartbeat",
                "build": {"version": "1.0"},
                "device_actions": ["agent_restart"],
                "management": dict(info["management"]),
                "scheduler_error": "",
                "scheduler": {
                    "reason": "healthy",
                    "running": active_exec,
                    "queued": 0,
                    "lanes": {
                        "execution": {"running": active_exec, "queued": 0, "capacity": 4},
                        "read": {"running": 0, "queued": 0, "capacity": 8},
                        "remote": {"running": 0, "queued": 0, "capacity": 4},
                    },
                    "resource_semantics": "strict",
                },
            }
            ok, _ = runtime._receive_agent_frame("dev", conn, info, payload)
            assert ok

        device_writes = [s for s in statements if "UPDATE devices" in s or "UPDATE \"devices\"" in s]
        assert device_writes == []
        assert store.security_epoch() == epoch_before
        assert store.session_revision == rev_before
        assert conn.runtime_snapshot is not None
        assert conn.runtime_snapshot["lanes"]["execution"]["running"] == 2
        assert "scheduler" not in json.loads(
            store.one("SELECT info FROM devices WHERE id='dev'")["info"]
        )

        # Readers consult in-memory runtime_snapshot & online() without hitting SQL for online().
        statements.clear()
        assert runtime.online("dev") is True
        assert statements == []
        with store.lock:
            store.db.set_trace_callback(None)

        admin = Principal("panel:admin", "owner", {"read", "write", "execute"}, ["*"], admin=True)
        sched = scheduler_settings_view(store, runtime, admin, "dev")
        assert sched["reported"]["lanes"]["execution"]["running"] == 2

        # Touch within 60s window avoids DB writes; after 60s writes ONLY device_presence.
        conn.last_seen = conn._persisted_last_seen = time.time() - 61
        runtime._touch_agent("dev", conn)
        presence_row = store.one("SELECT last_seen FROM device_presence WHERE device_id='dev'")
        assert presence_row is not None
        assert presence_row["last_seen"] >= conn.last_seen - 1
        assert store.security_epoch() == epoch_before
        assert store.session_revision == rev_before

        # Real readers check runtime.device_last_seen (in-memory -> device_presence -> devices).
        listed = device_rows(store, runtime, admin)
        assert listed[0]["last_seen"] == conn.last_seen
        diag = Diagnostics(runtime).get({"project": None, "client_catalog_sha256": None}, admin)
        assert diag["devices"][0]["last_seen"] == conn.last_seen

        # Non-security metadata write to `meta` bumps config_revision without security_epoch churn.
        cfg_before = store.config_revision
        store.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('public_url','https://x.test')")
        assert store.config_revision > cfg_before
        assert store.security_epoch() == epoch_before
        assert store.session_revision == rev_before

        # Redline #1: security-critical changes on devices, projects, grants & grant_expirations
        # MUST synchronously bump BOTH security_epoch and session_revision.
        store.execute("UPDATE devices SET enabled=0 WHERE id='dev'")
        e1, r1 = store.security_epoch(), store.session_revision
        assert e1 > epoch_before and r1 > rev_before

        store.execute("UPDATE devices SET secret=? WHERE id='dev'", (store.encrypt("new-secret"),))
        e2, r2 = store.security_epoch(), store.session_revision
        assert e2 > e1 and r2 > r1

        _grant_row(store, "g-exp", "owner", ["read"])
        e3, r3 = store.security_epoch(), store.session_revision
        assert e3 > e2 and r3 > r2

        store.execute("INSERT INTO grant_expirations(grant_id,expires,revision) VALUES('g-exp',?,1)", (time.time() + 3600,))
        e4, r4 = store.security_epoch(), store.session_revision
        assert e4 > e3 and r4 > r3
        update_grant_expiry(store, admin, "g-exp", time.time() + 7200, 1, confirm_reactivate=True)
        assert store.security_epoch() > e4
        assert store.session_revision > r4
    finally:
        store.close()


@pytest.mark.asyncio
async def test_store_synchronous_normal_override_and_operations_read_decoupling(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEPIER_SQLITE_SYNCHRONOUS", raising=False)
    store_default = Store(tmp_path / "default_hub")
    try:
        with store_default.lock:
            mode = store_default.db.execute("PRAGMA synchronous").fetchone()[0]
            journal = store_default.db.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal.lower() == "wal"
        assert mode == 2  # FULL
    finally:
        store_default.close()

    monkeypatch.setenv("CODEPIER_SQLITE_SYNCHRONOUS", "NORMAL")
    store_full = Store(tmp_path / "full_hub")
    try:
        with store_full.lock:
            mode = store_full.db.execute("PRAGMA synchronous").fetchone()[0]
        assert mode == 1  # NORMAL
    finally:
        store_full.close()

    monkeypatch.setenv("CODEPIER_SQLITE_SYNCHRONOUS", "OFF")
    with pytest.raises(ValueError, match="CODEPIER_SQLITE_SYNCHRONOUS"):
        Store(tmp_path / "invalid_hub")
    monkeypatch.delenv("CODEPIER_SQLITE_SYNCHRONOUS", raising=False)

    # Routine business-state reads of operations ignore concurrent non-security operations writes,
    # while security table writes still force retry to preserve epoch consistency.
    store, _ = _seed_hub(tmp_path / "decouple")
    try:
        store.execute(
            "INSERT INTO operations(id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated,space_id,owner_user_id) "
            "VALUES ('op1','dev','proj','panel:admin','fs_read','{}','fp1','queued',1,1,'legacy','owner')"
        )
        attempts = 0

        def read_operation_joined():
            nonlocal attempts
            attempts += 1
            row = store.one(
                "SELECT o.id, o.state, p.alias, d.enabled FROM operations o "
                "JOIN projects p ON p.id=o.project_id JOIN devices d ON d.id=o.device_id WHERE o.id='op1'"
            )
            if attempts == 1:
                with sqlite3.connect(store.state_directory / "hub.sqlite3") as external:
                    external.execute("UPDATE operations SET state='running', updated=2 WHERE id='op1'")
            return row

        first = await store.run_read(read_operation_joined)
        assert attempts == 1
        assert first["state"] == "queued"

        sec_attempts = 0

        def read_with_security_commit():
            nonlocal sec_attempts
            sec_attempts += 1
            row = store.one("SELECT d.enabled FROM devices d WHERE d.id='dev'")
            if sec_attempts == 1:
                with sqlite3.connect(store.state_directory / "hub.sqlite3") as external:
                    external.execute("UPDATE devices SET enabled=0 WHERE id='dev'")
            return row

        res = await store.run_read(read_with_security_commit)
        assert sec_attempts == 2
        assert res["enabled"] == 0

        # External schema edit (not via Store._writer_db) MUST still invalidate _epoch_schema and retry.
        ext_ddl_attempts = 0

        def read_after_external_ddl():
            nonlocal ext_ddl_attempts
            ext_ddl_attempts += 1
            row = store.one("SELECT id, state FROM operations WHERE id='op1'")
            if ext_ddl_attempts == 1:
                with sqlite3.connect(store.state_directory / "hub.sqlite3") as external:
                    external.execute("CREATE TABLE IF NOT EXISTS external_ddl_probe(x INTEGER)")
                    external.execute("UPDATE operations SET state='reconnecting' WHERE id='op1'")
            return row

        after_ddl = await store.run_read(read_after_external_ddl)
        assert ext_ddl_attempts == 2
        assert after_ddl["state"] == "reconnecting"
    finally:
        await store.aclose()


def test_integrations_single_commit_batch_overlap_and_pure_cpu_capture_output(tmp_path):
    store, runtime = _seed_hub(tmp_path)
    try:
        integrations = runtime.integrations
        principal = Principal("panel:admin", "owner", {"read", "write", "execute"}, ["*"], admin=True)
        project = runtime.project("ProjectAlpha", principal)
        meta = {"openai/session": "win-1"}

        statements: list[str] = []
        commits = 0
        orig_tx = store.transaction

        class CountingTx:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                return self._inner.__enter__()

            def __exit__(self, *exc):
                nonlocal commits
                try:
                    return self._inner.__exit__(*exc)
                finally:
                    if exc[0] is None:
                        commits += 1

        # Start 3 overlapping calls in the same host window, then 1 more to test batched overlap.
        integrations.begin(principal, project, "fs_read", meta, {"project": "ProjectAlpha", "path": "a"})
        integrations.begin(principal, project, "fs_read", meta, {"project": "ProjectAlpha", "path": "b"})
        integrations.begin(principal, project, "fs_read", meta, {"project": "ProjectAlpha", "path": "c"})

        executed_sql: list[str] = []
        orig_execute = store.execute

        def spy_execute(sql: str, args=(), *, security_write=None):
            executed_sql.append(sql)
            return orig_execute(sql, args, security_write=security_write)

        store.execute = spy_execute
        try:
            t4 = integrations.begin(principal, project, "fs_read", meta, {"project": "ProjectAlpha", "path": "d"})
        finally:
            store.execute = orig_execute

        overlap_updates = [s for s in executed_sql if "UPDATE mcp_activity SET transition='overlap'" in s]
        assert len(overlap_updates) == 1
        assert "WHERE id IN (" in overlap_updates[0]
        assert t4["transition"] == "overlap"

        # capture_output must execute ZERO SQL and propagate dual token metrics without plaintext.
        statements.clear()
        with store.lock:
            store.db.set_trace_callback(statements.append)
        secret_body = "SECRET_SOURCE_TEXT_DO_NOT_PERSIST"
        integrations.capture_output(
            t4,
            {
                "content": [{"type": "text", "text": json.dumps({"content": secret_body})}],
                "structuredContent": {"content": secret_body},
            },
        )
        with store.lock:
            store.db.set_trace_callback(None)
        assert statements == []
        usage = t4["token_usage"]
        assert isinstance(usage["wire_tokens_est"], int) and usage["wire_tokens_est"] > 0
        assert isinstance(usage["semantic_tokens_est"], int) and usage["semantic_tokens_est"] > 0
        assert isinstance(usage["mirror_overhead_ratio"], float)
        assert secret_body not in json.dumps(usage)

        # finish() uses <= 1 transaction commit and skips per-call DELETE FROM mcp_activity prune.
        statements.clear()
        commits = 0
        store.transaction = lambda **kw: CountingTx(orig_tx(**kw))
        try:
            with store.lock:
                store.db.set_trace_callback(statements.append)
            integrations.finish(t4)
            with store.lock:
                store.db.set_trace_callback(None)
        finally:
            store.transaction = orig_tx
        assert commits <= 1
        assert not any("DELETE FROM mcp_activity" in s for s in statements)
        recorded = store.one("SELECT metrics FROM mcp_usage WHERE activity_id=?", (t4["id"],))
        assert recorded is not None
        assert secret_body not in recorded["metrics"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_readonly_principal_can_cancel_own_slow_reads_including_knowledge(tmp_path, monkeypatch):
    store, runtime = _seed_hub(tmp_path)
    try:
        _grant_row(store, "g-read-owner", "owner", ["read"])
        _grant_row(store, "g-read-owner-2", "owner", ["read"])
        _grant_row(store, "g-read-peer", "peer", ["read"])
        _grant_row(store, "g-exec-owner", "owner", ["read", "write", "execute"])

        p_read = Principal("mcp:g-read-owner:reader", "owner", {"read"}, ["*"], grant_id="g-read-owner")
        p_other_grant = Principal("mcp:g-read-owner-2:reader2", "owner", {"read"}, ["*"], grant_id="g-read-owner-2")
        p_peer = Principal("mcp:g-read-peer:peer", "peer", {"read"}, ["*"], grant_id="g-read-peer")
        p_exec = Principal("mcp:g-exec-owner:exec", "owner", {"read", "write", "execute"}, ["*"], grant_id="g-exec-owner")

        assert {"open_workspace", "project_knowledge_query", "project_knowledge_refresh", "searches_start"} <= CANCELLABLE_READ_TOOLS
        assert "project_knowledge_build" not in CANCELLABLE_READ_TOOLS
        assert "shell_exec" not in CANCELLABLE_READ_TOOLS

        def seed_op(op_id: str, tool: str, *, principal: Principal, attempts: int = 1, args=None):
            store.execute(
                "INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,attempts,created,updated,space_id,owner_user_id,visibility) "
                "VALUES (?,?,?,?,?,?,?,?, 'running', ?, ?, ?, ?, ?, 'space')",
                (
                    op_id,
                    "dev",
                    "proj",
                    principal.actor,
                    principal.grant_id,
                    tool,
                    json.dumps(args or {"project": "ProjectAlpha"}),
                    f"fp-{op_id}",
                    attempts,
                    time.time(),
                    time.time(),
                    principal.space_id,
                    principal.user_id,
                ),
            )

        # Same read-only principal can cancel own in-flight slow reads including knowledge & search.
        for idx, tool in enumerate(
            ("fs_read", "open_workspace", "project_knowledge_query", "project_knowledge_refresh", "searches_start")
        ):
            op_id = f"read-op-{idx}"
            seed_op(op_id, tool, principal=p_read, attempts=1)
            res = await runtime.invoke("operations_cancel", {"operation_id": op_id}, p_read)
            assert res["cancel_requested"] is True
            assert res["state"] == "cancelling"

        # Redline #3: cross-grant (even same user), cross-user, and write/exec ops require execute.
        seed_op("op-diff-grant", "open_workspace", principal=p_read, attempts=1)
        with pytest.raises(DevError) as exc_diff_grant:
            await runtime.invoke("operations_cancel", {"operation_id": "op-diff-grant"}, p_other_grant)
        assert exc_diff_grant.value.status == 403

        with pytest.raises(DevError) as exc_peer:
            await runtime.invoke("operations_cancel", {"operation_id": "op-diff-grant"}, p_peer)
        assert exc_peer.value.status == 404
        cross_exec = await runtime.invoke("operations_cancel", {"operation_id": "op-diff-grant"}, p_exec)
        assert cross_exec["cancel_requested"] is True

        seed_op("op-own-write", "project_knowledge_build", principal=p_read, attempts=0)
        with pytest.raises(DevError) as exc_write:
            await runtime.invoke("operations_cancel", {"operation_id": "op-own-write"}, p_read)
        assert exc_write.value.status == 403

        seed_op("op-own-exec", "shell_exec", principal=p_read, attempts=1)
        with pytest.raises(DevError) as exc_exec:
            await runtime.invoke("operations_cancel", {"operation_id": "op-own-exec"}, p_read)
        assert exc_exec.value.status == 403

        seed_op("op-probe", "computer_status", principal=p_read, attempts=1, args={"project": "ProjectAlpha", "probe": True})
        with pytest.raises(DevError) as exc_probe:
            await runtime.invoke("operations_cancel", {"operation_id": "op-probe"}, p_read)
        assert exc_probe.value.status == 403

        # Room-scope fail-closed alignment: own read op permitted with read capability; write/cross-grant denied.
        record = {
            "grant_id": p_read.grant_id,
            "space_id": "legacy",
            "owner_user_id": "owner",
            "manifest": {
                "execution_mode": "file_tools_only",
                "project_rules": [
                    {"project_id": "proj", "capabilities": ["read"], "execution_targets": ["agent"]}
                ],
            },
        }
        monkeypatch.setattr(
            room_scope,
            "current",
            lambda _store, grant_id, **_kw: record if grant_id == p_read.grant_id else None,
        )
        seed_op("op-room-read", "project_knowledge_query", principal=p_read, attempts=1)
        room_ok = await runtime.invoke("operations_cancel", {"operation_id": "op-room-read"}, p_read)
        assert room_ok["cancel_requested"] is True

        seed_op("op-room-other", "project_knowledge_query", principal=p_other_grant, attempts=1)
        with pytest.raises(DevError):
            await runtime.invoke("operations_cancel", {"operation_id": "op-room-other"}, p_read)

        seed_op("op-room-exec", "shell_exec", principal=p_read, attempts=1)
        with pytest.raises(DevError):
            await runtime.invoke("operations_cancel", {"operation_id": "op-room-exec"}, p_read)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_ws_large_payload_does_not_kill_sibling_inflight_operations(monkeypatch):
    # 1. Crypto thread-pool offload for > 64 KiB + sequence ordering under concurrency.
    secret = shared_crypto.token(32)
    challenge = shared_crypto.token(32)
    hub_ch = SecureChannel(secret, challenge, "dev", "hub")
    agent_ch = SecureChannel(secret, challenge, "dev", "agent")

    to_thread_calls = 0
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        nonlocal to_thread_calls
        to_thread_calls += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(shared_crypto.asyncio, "to_thread", spy_to_thread)

    small_wire, large_wire = await asyncio.gather(
        pack_channel(hub_ch, {"type": "ack", "id": "small"}),
        pack_channel(hub_ch, {"type": "call", "id": "big", "blob": "x" * (ASYNC_OFFLOAD_BYTES + 4096)}),
    )
    assert to_thread_calls == 1
    assert (await unpack_channel(agent_ch, small_wire))["id"] == "small"
    assert (await unpack_channel(agent_ch, large_wire))["id"] == "big"
    assert to_thread_calls == 2

    # 2. Connection decoupled priority queue & dynamic timeout (>10s + payload_mb*2)
    # ensures large outbound data frame does NOT starve control frames or kill sibling inflight work.
    class FakeWebSocket:
        def __init__(self):
            self.sent: list[dict] = []
            self.large_started = asyncio.Event()
            self.release_large = asyncio.Event()
            self.closed_codes: list[int] = []
            self.timeouts: list[float] = []

        async def send_text(self, text: str):
            msg = agent_ch.unpack(text)
            self.sent.append(msg)
            if msg.get("id") == "big-call":
                self.large_started.set()
                await self.release_large.wait()

        async def close(self, code: int = 1000, reason: str = ""):
            self.closed_codes.append(code)

    ws = FakeWebSocket()
    conn = Connection(ws, hub_ch)

    real_wait_for = asyncio.wait_for

    async def spy_wait_for(aw, *args, **kwargs):
        wait_timeout = kwargs.get("timeout", args[0] if args else None)
        if getattr(getattr(aw, "cr_code", None), "co_name", None) == "send_text" and wait_timeout is not None:
            ws.timeouts.append(wait_timeout)
        return await real_wait_for(aw, *args, **kwargs)

    monkeypatch.setattr("hub.runtime.asyncio.wait_for", spy_wait_for)

    try:
        # Send large >64 KiB call; writer blocks waiting on release_large,
        # simulating a slow large frame (>4s in production).
        large_task = asyncio.create_task(
            conn.send({"type": "call", "id": "big-call", "blob": "z" * (ASYNC_OFFLOAD_BYTES + 65536)})
        )
        await asyncio.wait_for(ws.large_started.wait(), 2)

        # Sibling control frames (ack / cancel) enqueue non-blocking and are accepted while data queues wait.
        queued_data_task = asyncio.create_task(conn.send({"type": "call", "id": "second-data"}))
        assert conn.send_nowait({"type": "ack", "id": "sibling-ack"}) is True
        cancel_task = asyncio.create_task(conn.send({"type": "cancel", "id": "sibling-cancel"}))

        ws.release_large.set()
        assert await asyncio.wait_for(large_task, 2) is True
        assert await asyncio.wait_for(cancel_task, 2) is True
        assert await asyncio.wait_for(queued_data_task, 2) is True

        # Tunnel stayed open & usable; timeout was dynamically > 10s (never 4s); control preempted data!
        assert conn.unusable is False
        assert ws.closed_codes == []
        assert all(t >= 10.0 for t in ws.timeouts)
        ids_in_order = [m.get("id") for m in ws.sent]
        assert ids_in_order.index("sibling-ack") < ids_in_order.index("second-data")
        assert ids_in_order.index("sibling-cancel") < ids_in_order.index("second-data")
    finally:
        ws.release_large.set()
        conn.close()


@pytest.mark.asyncio
async def test_device_dispatch_lock_decoupled_from_prepare_and_wait_for_operation(tmp_path, monkeypatch):
    store, runtime = _seed_hub(tmp_path)
    try:
        conn = SimpleNamespace(
            last_seen=time.time(),
            unusable=False,
            journal_id="journal-1",
            protocol=2,
            tool_protocol=None,
            cancel_pending_protocol=0,
            resource_semantics_protocol=0,
            file_options_protocol=0,
            queue_deadline_protocol=0,
            project_view_protocol=0,
            project_runtime_protocol=0,
            project_runtime_profiles=[],
            bounded_execution_protocol=0,
        )
        runtime.connections["dev"] = conn
        runtime.connection_authorized = lambda device_id, c: c is conn

        p = Principal("panel:admin", "owner", {"read", "write", "execute"}, ["*"], admin=True)
        op_id = (
            await runtime.invoke(
                "fs_read", {"project": "ProjectAlpha", "path": "README.md"}, p
            )
        )["operation_id"]

        # Holding device_dispatch_lock does NOT block _prepare_for_send or cancel.
        async with runtime.device_dispatch_lock("dev"):
            prepared = await asyncio.wait_for(
                runtime._prepare_for_send(op_id, device_id="dev"), timeout=1
            )
            assert prepared is not None and prepared[2][0]["type"] == "call"

        # Holding device_delivery_lock does NOT block dispatch or cancel.
        async with runtime.device_delivery_lock("dev"):
            second = await asyncio.wait_for(
                runtime.invoke("fs_read", {"project": "ProjectAlpha", "path": "other.md"}, p),
                timeout=1,
            )
            assert second["operation_id"] != op_id
            cancelled = await asyncio.wait_for(
                runtime.cancel(second["operation_id"], p), timeout=1
            )
            assert cancelled["cancel_requested"] is True

        # wait_for_operation supports cancel_event and immediate wakeup on completion.
        cancel_ev = asyncio.Event()
        cancel_ev.set()
        assert await runtime.wait_for_operation(op_id, 5.0, cancel_event=cancel_ev) is None

        waiter = asyncio.create_task(runtime.wait_for_operation(op_id, 5.0))
        await asyncio.sleep(0.02)
        await runtime.store.run(runtime.complete, {"id": op_id}, {"ok": True, "data": {"content": "ok"}})
        done_row = await asyncio.wait_for(waiter, timeout=2)
        assert done_row is not None and done_row["state"] == "succeeded"

        # RuntimeConfig rolling delivery defaults off and supports explicit opt-in.
        monkeypatch.delenv("HUB_DELIVERY_ROLLING_DEVICES", raising=False)
        cfg = RuntimeConfig.from_env()
        assert cfg.delivery_rolling_devices == ()
        assert cfg.rolling_for("any-device") is False
        monkeypatch.setenv("HUB_DELIVERY_ROLLING_DEVICES", "*")
        assert RuntimeConfig.from_env().rolling_for("any-device") is True
        monkeypatch.setenv("HUB_DELIVERY_ROLLING_DEVICES", "")
        assert RuntimeConfig.from_env().rolling_for("any-device") is False
    finally:
        await runtime.stop()
        await store.aclose()
