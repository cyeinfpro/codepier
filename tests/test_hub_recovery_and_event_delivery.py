"""Independent synthetic regressions for scan S01-S03."""
import asyncio
import json
import os
import time
from types import SimpleNamespace
import pytest
from cryptography.fernet import InvalidToken
from hub import keyring, operation_history
from hub.auth import Auth
from hub.runtime import Runtime
from hub.store import Store
from shared.util import DevError

@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "hub")
    try:
        yield value
    finally:
        value.close()

def test_existing_account_survives_probe_saturation(store, monkeypatch):
    import hub.auth as module
    monkeypatch.setattr(module, "LOGIN_ATTEMPT_STORAGE_LIMIT", 2)
    store.execute("INSERT INTO users VALUES ('owner','owner','unused',1)")
    auth = Auth(store)
    def request(ip):
        return SimpleNamespace(client=SimpleNamespace(host=ip))
    for n in range(2):
        auth._login_attempt(request(str(n)), "unknown-" + str(n))
    with pytest.raises(DevError):
        auth._login_attempt(request("new"), "unknown-new")
    for n in range(module.LOGIN_ACCOUNT_ATTEMPT_LIMIT):
        _, user = auth._login_attempt(request("known-" + str(n)), "owner")
        assert user["id"] == "owner"
    with pytest.raises(DevError) as exc:
        auth._login_attempt(request("fresh"), "owner")
    assert exc.value.code == "LOGIN_THROTTLED"

@pytest.mark.skipif(os.name == "nt", reason="POSIX key mode contract")
def test_master_key_rejects_public_mode(tmp_path):
    directory = tmp_path / "hub"
    Store(directory).close()
    (directory / "master.key").chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        Store(directory)

def test_inventory_detects_unregistered_cipher_column(store):
    store.execute("CREATE TABLE forgotten_secrets(id TEXT PRIMARY KEY, body TEXT)")
    store.execute("INSERT INTO forgotten_secrets VALUES ('one',?)", (store.encrypt("synthetic-secret"),))
    with pytest.raises(RuntimeError, match="forgotten_secrets.body"):
        keyring.verify_cipher_inventory(store)

def test_channel_rotation_independent_inventory(tmp_path):
    directory = tmp_path / "hub"
    store = Store(directory)
    expected = {
        "weixin_accounts": ("secret", "cursor"),
        "weixin_bindings": ("pairing_secret",),
        "weixin_contexts": ("token",),
        "weixin_enrollments": ("payload",),
        "weixin_deliveries": (),
        "weixin_parts": ("body", "wire_body"),
        "telegram_accounts": ("secret",),
        "telegram_bindings": ("pairing_secret",),
        "telegram_inbox": ("body",),
        "channel_onboarding_notices": ("body",),
        "room_weixin_logins": ("payload",),
    }
    try:
        # Synthetic rows exercise the real ciphertext columns without external channels.
        with store.lock:
            for table, encrypted in expected.items():
                values = {}
                for column in store.db.execute(f"PRAGMA table_info({table})").fetchall():
                    name, kind, required, default, primary = column[1:6]
                    if name in encrypted:
                        values[name] = store.encrypt(table + "." + name)
                    elif (required and default is None) or primary:
                        values[name] = 1 if kind in {"INTEGER", "REAL"} else "synthetic"
                store.db.execute(f"INSERT INTO {table}({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
            store.db.execute("COMMIT")
    finally:
        store.close()
    keyring.rotate_key(directory)
    reopened = Store(directory)
    try:
        for table, columns in expected.items():
            row = reopened.one(f"SELECT * FROM {table}")
            for column in columns:
                assert reopened.decrypt(row[column]) == table + "." + column
    finally:
        reopened.close()

@pytest.mark.parametrize("value", ["history:v1:ordinary output", "archive:v1:ordinary output"])
def test_legacy_prefix_read_and_migration(store, value):
    store.execute("""INSERT INTO operations(id,actor,tool,args_summary,fingerprint,state,created,updated,output)
        VALUES ('legacy','test','exec','{}','fp','succeeded',1,1,?)""", (value,))
    assert operation_history.unseal(store, value) == value
    assert operation_history.encrypt_legacy_history(store)["operations.output"] == 1
    saved = store.one("SELECT output FROM operations WHERE id='legacy'")["output"]
    assert saved != value and operation_history.unseal(store, saved) == value

def test_corrupt_ciphertext_is_not_plaintext(store):
    with pytest.raises(InvalidToken):
        operation_history.unseal(store, "history:v1:gAAAA-corrupt")

def test_queue_overflow_requires_resync():
    queue = asyncio.Queue(maxsize=2)
    queue.put_nowait({"type": "operation"})
    queue.put_nowait({"type": "output"})
    Runtime._enqueue(queue, {"type": "operation", "data": {"state": "succeeded"}})
    assert queue.qsize() == 1
    assert queue.get_nowait() == {"type": "resync", "data": {}}

def test_frames_authenticate_once_and_coalesce_last_seen(store):
    runtime = Runtime.__new__(Runtime)
    connection = SimpleNamespace(last_seen=0)
    runtime.connections = {"device": connection}
    runtime.store = store
    calls = []
    runtime.connection_authorized = lambda device, current: calls.append((device, current)) or True
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('device','fixture','unused',1)")
    for _ in range(30):
        assert runtime._receive_agent_frame("device", connection, {}, {"type": "unknown"})[0]
    assert len(calls) == 30
    persisted = store.one("SELECT last_seen FROM device_presence WHERE device_id='device'")["last_seen"]
    assert persisted <= connection.last_seen
    assert connection._persisted_last_seen == persisted
    runtime.connection_authorized = lambda *args: False
    assert runtime._receive_agent_frame("device", connection, {}, {"type": "unknown"}) == (False, None)

def test_artifact_retention_uses_hub_clock(store):
    from hub.artifacts import ArtifactService
    service = ArtifactService(SimpleNamespace(store=store))
    store.execute("INSERT INTO users VALUES ('owner','owner','unused',1)")
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('device','fixture','unused',1)")
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,created) VALUES ('project','fixture','fixture','device','/synthetic',1)")
    now = time.time()
    op = {"id": "artifact", "project_id": "project", "device_id": "device", "grant_id": None,
          "actor": "test", "space_id": "legacy", "owner_user_id": "owner",
          "payload": store.encrypt(json.dumps({"project": {"root": "/synthetic"}, "args": {}}))}
    service.register_result(op, {"artifact_id": "artifact", "name": "fixture.txt", "bytes": 1,
                                "sha256": "a"*64, "created": now+10**9, "expires": now+10**9+86400})
    saved = store.one("SELECT * FROM artifacts WHERE id='artifact'")
    assert now <= saved["created"] <= time.time()
    assert saved["expires"] <= time.time()+86400


@pytest.mark.asyncio
async def test_maintenance_failure_does_not_block_delivery():
    from hub.delivery_metrics import DeliveryMetrics
    runtime = Runtime.__new__(Runtime)
    runtime.stopping = False
    runtime.wake = asyncio.Event()
    runtime.delivery_metrics = DeliveryMetrics()
    observed = []
    async def run(call, *args, **kwargs):
        return call(*args, **kwargs)
    runtime.store = SimpleNamespace(run=run, run_read=run, run_hint_read=run,
                                    audit=lambda *args, **kwargs: observed.append("audit"))
    def purge(now, *, legacy):
        if legacy:
            observed.append("purge")
            raise RuntimeError("synthetic failure")
        observed.append("purge-sealed")
        return 0
    def candidates(active):
        observed.append("delivery")
        runtime.stopping = True
        return []
    runtime._purge_computer = purge
    runtime._delivery_candidates = candidates
    await runtime._run_maintenance()
    assert observed == ["purge", "audit", "purge-sealed"]
    assert runtime.delivery_metrics.snapshot()["maintenance_ms"]["p50"] is not None
    # Candidate selection runs without first waiting for periodic cleanup.
    await runtime.delivery_loop()
    assert observed == ["purge", "audit", "purge-sealed", "delivery"]

@pytest.mark.asyncio
async def test_external_writer_snapshot_churn_returns_retryable_error(store):
    other = Store(store.directory)
    attempts = []
    try:
        def query():
            store.one("SELECT count(*) AS n FROM users")
            attempts.append(1)
            other.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('churn',?)", (str(len(attempts)),))
            return "stale"
        with pytest.raises(DevError) as exc:
            await store.run_read(query)
        assert exc.value.code == "AUTHORIZATION_CHANGED"
        assert exc.value.status == 503 and exc.value.details["retry_after"] == 1
        assert len(attempts) == 4
    finally:
        other.close()

@pytest.mark.parametrize('value,error', [
    ('history:v1:gAAAA-corrupt', InvalidToken),
    ('archive:v1:missing:output', RuntimeError),
])
def test_ambiguous_legacy_envelopes_fail_closed_but_new_writes_are_unambiguous(store, value, error):
    # No provenance can distinguish old literal text shaped exactly like an
    # envelope from damaged storage. Never guess or hide authentication errors.
    with pytest.raises(error):
        operation_history.unseal(store, value)
    sealed = operation_history.seal(store, value)
    assert sealed != value
    assert operation_history.unseal(store, sealed) == value


@pytest.mark.asyncio
async def test_cancelled_device_send_does_not_cancel_delivery_loop():
    from hub.delivery_metrics import DeliveryMetrics
    runtime = Runtime.__new__(Runtime)
    runtime.stopping = False
    runtime.wake = asyncio.Event()
    runtime.delivery_metrics = DeliveryMetrics()
    first_started, second_started = asyncio.Event(), asyncio.Event()
    second_stopped, blocker = asyncio.Event(), asyncio.Event()
    device_tasks = {}
    selected = set()

    async def run(call, *args):
        return call(*args)

    runtime.store = SimpleNamespace(run_hint_read=run)

    def candidates(active):
        device = "first" if "first" not in selected else "second"
        if device in selected:
            return []
        selected.add(device)
        return [{"id": device + "-operation", "device_id": device}]

    async def deliver(ids, *, device_id, selected):
        device_tasks[device_id] = asyncio.current_task()
        (first_started if device_id == "first" else second_started).set()
        try:
            await blocker.wait()
        finally:
            if device_id == "second":
                second_stopped.set()

    runtime._delivery_candidates = candidates
    runtime.deliver_device = deliver
    worker = asyncio.create_task(runtime.delivery_loop())
    try:
        await asyncio.wait_for(first_started.wait(), 2)
        device_tasks["first"].cancel()  # An actual child task is cancelled by a detach.
        await asyncio.gather(device_tasks["first"], return_exceptions=True)
        await asyncio.wait_for(second_started.wait(), 2)
        assert not worker.done()
        # Cancelling the shared worker itself must still propagate and drain children.
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert second_stopped.is_set() and device_tasks["second"].cancelled()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def artifact_completion_fixture(store):
    store.execute("INSERT INTO users VALUES ('owner','owner','unused',1)")
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('device','fixture','unused',1)")
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,created) VALUES ('project','fixture','fixture','device','/original-root',1)")
    request = {
        "project": {"root": "/original-root", "private_context": "synthetic-context"},
        "args": {"path": "private-file.txt", "source_operation_id": "original-source"},
        "tool": "artifacts_register", "credential": "synthetic-must-not-be-retained",
    }
    now = time.time()
    store.execute("""INSERT INTO operations
        (id,project_id,device_id,actor,tool,args_summary,fingerprint,state,created,updated,payload,space_id,owner_user_id)
        VALUES ('late-artifact','project','device','fixture','artifacts_register','{}','fixture','running',?,?,?,'legacy','owner')""",
        (now, now, store.encrypt(json.dumps(request))))
    op = store.one("SELECT * FROM operations WHERE id='late-artifact'")
    receipt = {"artifact_id": op["id"], "name": "fixture.txt", "bytes": 1,
               "sha256": "a" * 64, "created": now, "expires": now + 86400}
    return Runtime(store), op, receipt


def test_late_artifact_receipt_keeps_original_binding_after_review_and_project_remap(store):
    runtime, original, receipt = artifact_completion_fixture(store)
    runtime.complete(original, {"ok": False, "error": {
        "code": "JOURNAL_CHANGED", "message": "synthetic receipt awaiting reconciliation"}})
    pending = store.one("SELECT * FROM operations WHERE id=?", (original["id"],))
    assert pending["state"] == "needs_review" and isinstance(pending["payload"], str)
    assert json.loads(store.decrypt(pending["payload"])) == {
        "project": {"root": "/original-root"},
        "args": {"source_operation_id": "original-source"},
    }
    store.execute("UPDATE projects SET root='/remapped-root' WHERE id='project'")
    runtime.complete(original, {"ok": True, "data": receipt})  # Deliberately stale caller row.
    completed = store.one("SELECT * FROM operations WHERE id=?", (original["id"],))
    saved = store.one("SELECT * FROM artifacts WHERE id=?", (original["id"],))
    assert completed["state"] == "succeeded" and completed["payload"] is None
    assert saved["root"] == "/original-root" and saved["source_operation_id"] == "original-source"
    runtime.complete(original, {"ok": True, "data": receipt})
    assert store.one("SELECT count(*) AS n FROM artifacts")["n"] == 1


@pytest.mark.parametrize("binding", ["missing", "corrupt", "malformed"])
def test_artifact_without_original_binding_keeps_safe_receipt_without_publishing(store, binding):
    runtime, original, receipt = artifact_completion_fixture(store)
    payload = {"missing": None, "corrupt": "gAAAA-corrupt",
               "malformed": store.encrypt(json.dumps({"project": {"root": ""}, "args": {}}))}[binding]
    store.execute("UPDATE operations SET state='needs_review',payload=? WHERE id=?",
                  (payload, original["id"]))
    runtime.complete(original, {"ok": True, "data": {**receipt, "private_extra": "not-a-public-receipt"}})
    saved = store.one("SELECT * FROM operations WHERE id=?", (original["id"],))
    result = json.loads(operation_history.unseal(store, saved["result"]))
    assert saved["state"] == "needs_review" and saved["payload"] is None
    assert result["error"]["code"] == "ARTIFACT_BINDING_UNAVAILABLE"
    assert result["error"]["artifact_receipt"] == receipt
    assert store.one("SELECT count(*) AS n FROM artifacts")["n"] == 0
    runtime.complete(original, {"ok": True, "data": receipt})
    assert store.one("SELECT count(*) AS n FROM operations")["n"] == 1
