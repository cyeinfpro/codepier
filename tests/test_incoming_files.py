"""In-process HTTP ingress with real sessions, grants, IAM and SQLite.

Only Agent invocation is simulated. No network, production gates or credentials
are changed by these disposable tests.
"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import uuid
import time

import pytest

from hub.incoming_files import CHUNK, MAX_BYTES, MAX_JSON, IncomingFileService, make_incoming_file_router
from shared.util import DevError
from tests.test_iam_integration import team as team, shared_role


def metadata(raw=b"", **changes):
    return {"project": "project-team", "idempotency_key": uuid.uuid4().hex,
            "path": "导入/空文件.weird", "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), **changes}


@pytest.fixture
def ingress(team, monkeypatch):
    app, browsers = team
    monkeypatch.setenv("CODEPIER_FILE_IMPORT_STREAMING", "true")
    runtime = app.state.runtime
    if not any(getattr(route, "path", None) == "/api/file-imports" for route in app.routes):
        app.include_router(make_incoming_file_router(app.state.auth, runtime))
    service = runtime.incoming_files
    calls, receipts, uploads = [], {}, {}
    state = {"pending": False, "after": None}

    async def invoke(name, args, principal):
        args = dict(args)
        def admit():
            project = runtime.project(args["project"], principal)
            with runtime.store.transaction(immediate=True):
                service.validate_admission(name, args, project, principal)
        await runtime.store.run(admit)
        key = (principal.space_id, principal.actor, args.get("idempotency_key") or uuid.uuid4().hex)
        op = receipts.setdefault(key, uuid.uuid4().hex)
        calls.append((name, args, principal))
        if name == "incoming_upload_begin":
            uploads.setdefault(op, {"upload_id": op, "path": args["path"], "bytes": args["size"],
                "sha256": args["sha256"], "received": 0, "state": "receiving",
                "ready": False, "created": False, "expires": time.time() + 86400})
            result = dict(uploads[op])
        else:
            result = uploads[args["upload_id"]]
            if name == "incoming_upload_chunk":
                result["received"] = max(result["received"], args["offset"] + len(base64.b64decode(args["data"])))
                result["ready"] = result["received"] == result["bytes"]
            if name == "incoming_upload_finish":
                result["state"] = "complete"
                result["ready"] = True
                result["created"] = True
            result = dict(result)
        await asyncio.sleep(0)
        if state["after"]:
            state["after"]()
        if state["pending"]:
            return {"operation_id": op, "pending": True, "state": "queued"}
        return {"operation_id": op, **result, "data": "never-echo-node-bytes", "output": "never-echo-node-output"}

    monkeypatch.setattr(runtime, "invoke", invoke)
    yield app, browsers, service, calls, state, uploads


def begin(b, raw=b"", **changes):
    request = metadata(raw, **changes)
    response = b.post("/api/file-imports", json=request)
    assert response.status_code == 200, response.text
    return request, response.json()


def test_gate_preserves_explicit_disable_and_rejects_malformed(ingress, monkeypatch):
    app, b, service, calls, _, _ = ingress
    for value in ("", "1", "TRUE", "false", " true"):
        if value is None:
            monkeypatch.delenv("CODEPIER_FILE_IMPORT_STREAMING")
        else:
            monkeypatch.setenv("CODEPIER_FILE_IMPORT_STREAMING", value)
        assert b["owner"].post("/api/file-imports", json=metadata()).status_code == 404
    assert not calls
    assert not app.state.store.all("SELECT * FROM incoming_file_imports")


def test_authentication_write_scope_and_panel_csrf(ingress):
    app, b, _, calls, _, _ = ingress
    assert b["owner"].client.post("/api/file-imports", json=metadata()).status_code == 401
    assert b["owner"].post("/api/file-imports", json=metadata(), headers={"X-RD-CSRF": "wrong"}).status_code == 403
    assert b["owner"].post("/api/file-imports", json=metadata(), headers={"Origin": "https://evil.invalid"}).status_code == 403
    shared_role(app, b, ["read"])
    assert b["alice"].post("/api/file-imports", json=metadata()).status_code == 403
    assert not calls


def test_zero_bytes_unicode_repeated_begin_finish_and_restart(ingress):
    app, b, service, calls, _, _ = ingress
    request, first = begin(b["owner"])
    assert first["bytes"] == 0 and not first["ready"]
    assert first["path"] == "导入/空文件.weird"
    repeated = b["owner"].post("/api/file-imports", json=request).json()
    assert repeated["upload_id"] == first["upload_id"] == repeated["operation_id"]
    assert len(app.state.store.all("SELECT * FROM incoming_file_imports")) == 1
    rebuilt = IncomingFileService(app.state.runtime)
    row = rebuilt.store.one("SELECT * FROM incoming_file_imports WHERE upload_id=?", (first["upload_id"],))
    assert row["bytes"] == 0 and row["path"] == request["path"]
    done = b["owner"].post("/api/file-imports/" + first["upload_id"] + "/finish")
    assert done.status_code == 200 and done.json()["state"] == "complete"
    assert not any(name == "incoming_upload_chunk" for name, _, _ in calls)
    assert service.store.one("SELECT state FROM incoming_file_imports")["state"] == "finished"


def test_raw_chunk_stable_key_hash_and_no_binary_in_receipt(ingress):
    app, b, _, calls, _, _ = ingress
    raw = b"NEVER-RENDER-THIS-BINARY\x00\xff" * 150
    _, first = begin(b["owner"], raw)
    path = "/api/file-imports/" + first["upload_id"] + "/chunks?offset=0"
    headers = {"Content-Type": "application/octet-stream", "X-Chunk-Sha256": hashlib.sha256(raw).hexdigest()}
    response = b["owner"].put(path, content=raw, headers=headers)
    repeated = b["owner"].put(path, content=raw, headers=headers)
    assert response.status_code == repeated.status_code == 200
    assert response.json()["operation_id"] == repeated.json()["operation_id"]
    assert response.json()["received"] == len(raw)
    assert calls[-1][1]["idempotency_key"] == calls[-2][1]["idempotency_key"]
    assert base64.b64decode(calls[-1][1]["data"]) == raw
    assert "data" not in response.json() and "output" not in response.json()
    assert "NEVER-RENDER" not in response.text
    assert base64.b64encode(raw).decode() not in response.text
    assert "no-store" in response.headers["cache-control"]
    bad = b["owner"].put(path, content=b"wrong", headers=headers)
    assert bad.status_code == 400
    assert app.state.store.one("SELECT COUNT(*) AS n FROM incoming_file_imports")["n"] == 1


def test_pending_begin_is_bound_and_retry_uses_same_receipt(ingress):
    _, b, service, _, state, _ = ingress
    state["pending"] = True
    request, first = begin(b["owner"], b"abc")
    assert first["pending"] and first["upload_id"] == first["operation_id"]
    assert first["next_call"]["arguments"]["operation_id"] == first["operation_id"]
    assert service.store.one("SELECT upload_id FROM incoming_file_imports")["upload_id"] == first["upload_id"]
    second = b["owner"].post("/api/file-imports", json=request).json()
    assert second["operation_id"] == first["operation_id"]


def test_conflicting_begin_metadata_rejected_before_agent(ingress):
    _, b, _, calls, _, _ = ingress
    request, _ = begin(b["owner"])
    n = len(calls)
    for changes in ({"path": "other"}, {"size": 1}, {"sha256": "a" * 64}, {"workspace_id": "1" * 32},
                    {"project": "project-legacy"}):
        response = b["owner"].post("/api/file-imports", json={**request, **changes})
        assert response.status_code in (404, 409)
    assert len(calls) == n


def test_exact_caller_space_project_and_workspace_binding(ingress):
    app, b, _, calls, _, _ = ingress
    shared_role(app, b, ["read", "write"])
    _, first = begin(b["owner"])
    path = "/api/file-imports/" + first["upload_id"]
    count = len(calls)
    assert b["alice"].get(path).status_code == 404
    assert b["legacy"].get(path).status_code == 404
    assert b["owner"].get(path + "?project=project-legacy").status_code == 409
    assert b["owner"].get(path + "?workspace_id=" + "a" * 32).status_code == 409
    assert b["owner"].get("/api/file-imports/" + "a" * 32).status_code == 404
    assert len(calls) == count


def test_bearer_uses_exact_grant_and_requires_no_execute(ingress):
    app, b, _, calls, _, _ = ingress
    def grant(scopes):
        response = b["owner"].post("/api/grants", json={"label": "upload fixture",
            "scopes": scopes, "projects": ["project-team"], "days": 1})
        assert response.status_code == 200, response.text
        return response.json()
    grant1, grant2 = grant(["read", "write"]), grant(["read", "write"])
    headers = {"Authorization": "Bearer " + grant1["token"], "X-RD-CSRF": "not-used"}
    response = b["owner"].post("/api/file-imports", json=metadata(), headers=headers)
    assert response.status_code == 200, response.text
    path = "/api/file-imports/" + response.json()["upload_id"]
    assert "execute" not in calls[-1][2].scopes
    assert b["owner"].get(path, headers={"Authorization": "Bearer " + grant2["token"]}).status_code == 404
    assert b["owner"].get(path).status_code == 404
    read = grant(["read"])
    assert b["owner"].get(path, headers={"Authorization": "Bearer " + read["token"]}).status_code == 403
    app.state.store.execute("UPDATE grants SET revoked=1 WHERE id=?", (grant1["grant_id"],))
    assert b["owner"].get(path, headers=headers).status_code == 401


@pytest.mark.parametrize("mutation", ["revoked", "root", "device", "read_only"])
def test_reauthorization_after_remote_wait(ingress, mutation):
    app, b, _, _, state, _ = ingress
    request = metadata()
    def change():
        if mutation == "revoked":
            app.state.store.execute("DELETE FROM sessions")
        elif mutation == "root":
            app.state.store.execute("UPDATE projects SET root=root||'/moved' WHERE id='project-team'")
        elif mutation == "device":
            app.state.store.execute("UPDATE devices SET enabled=0 WHERE id='device-team'")
        else:
            app.state.store.execute("UPDATE projects SET mode='read' WHERE id='project-team'")
    state["after"] = change
    response = b["owner"].post("/api/file-imports", json=request)
    assert response.status_code in (401, 403, 409)
    assert "upload_id" not in response.json()


def test_remapping_denied_before_agent_and_admission_hook(ingress):
    app, b, service, calls, _, _ = ingress
    request, first = begin(b["owner"], b"abc")
    row = app.state.store.one("SELECT * FROM incoming_file_imports")
    original = dict(calls[-1][1])
    principal = calls[-1][2]
    project = app.state.runtime.project("project-team", principal)
    app.state.store.execute("UPDATE projects SET root=root||'/moved' WHERE id='project-team'")
    n = len(calls)
    assert b["owner"].get("/api/file-imports/" + first["upload_id"]).status_code == 409
    assert len(calls) == n
    with pytest.raises(DevError) as exc:
        service.validate_admission("incoming_upload_begin", original, project, principal)
    assert exc.value.code == "FILE_IMPORT_MAPPING_CHANGED"
    assert row["root"] == project["root"]


@pytest.mark.parametrize("changes", [
    {"size": -1}, {"size": MAX_BYTES + 1}, {"size": True}, {"size": 1.5},
    {"sha256": "x" * 64}, {"path": "../escape"}, {"path": "/absolute"},
    {"path": "C:/absolute"}, {"path": "a\\b"}, {"url": "https://never-fetch.invalid"},
    {"workspace_id": "fake"},
])
def test_strict_metadata(ingress, changes):
    _, b, _, calls, _, _ = ingress
    assert b["owner"].post("/api/file-imports", json=metadata(**changes)).status_code == 400
    assert not calls


def test_json_and_actual_chunk_size_limits(ingress):
    _, b, _, calls, _, _ = ingress
    assert b["owner"].post("/api/file-imports", content=b" " * (MAX_JSON + 1)).status_code == 413
    _, first = begin(b["owner"], b"x" * (CHUNK + 1))
    path = "/api/file-imports/" + first["upload_id"] + "/chunks?offset=0"
    headers = {"X-Chunk-Sha256": hashlib.sha256(b"x" * (CHUNK + 1)).hexdigest()}
    n = len(calls)
    assert b["owner"].put(path, content=b"x" * (CHUNK + 1), headers=headers).status_code == 413
    # Chunked request lacks Content-Length; actual bytes are still bounded.
    assert b["owner"].put(path, content=iter([b"x" * (CHUNK + 1)]), headers=headers).status_code == 413
    assert len(calls) == n


def test_active_quota_does_not_block_same_key_resume(ingress, monkeypatch):
    import hub.incoming_files as module
    _, b, _, _, _, _ = ingress
    monkeypatch.setattr(module, "MAX_ACTIVE", 1)
    request, first = begin(b["owner"])
    assert b["owner"].post("/api/file-imports", json=metadata()).status_code == 429
    assert b["owner"].post("/api/file-imports", json=request).json()["upload_id"] == first["upload_id"]


def test_concurrent_identical_begin_has_one_manifest_and_operation(ingress):
    app, b, _, _, _, _ = ingress
    request = metadata()
    def submit(_):
        response = b["owner"].post("/api/file-imports", json=request)
        assert response.status_code == 200, response.text
        return response.json()["upload_id"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(submit, range(3)))
    assert len(set(ids)) == 1
    assert app.state.store.one("SELECT COUNT(*) AS n FROM incoming_file_imports")["n"] == 1


def test_begin_reservation_blocks_direct_unbound_rpc(ingress):
    app, b, service, calls, _, _ = ingress
    _, _ = begin(b["owner"])
    principal = calls[-1][2]
    args = metadata()
    project = app.state.runtime.project("project-team", principal)
    with pytest.raises(DevError) as exc:
        service.validate_admission("incoming_upload_begin", args, project, principal)
    assert exc.value.code == "FILE_IMPORT_NOT_FOUND"


def test_mid_body_revocation_prevents_chunk_dispatch(ingress):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    app, b, _, calls, _, _ = ingress
    raw = b"abcdef"
    _, first = begin(b["owner"], raw)
    mini = FastAPI()
    mini.include_router(make_incoming_file_router(app.state.auth, app.state.runtime))
    @mini.exception_handler(DevError)
    async def error(request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status)
    n = len(calls)
    async def scenario():
        received = 0
        messages = []
        async def receive():
            nonlocal received
            received += 1
            if received == 1:
                return {"type": "http.request", "body": raw[:3], "more_body": True}
            app.state.store.execute("DELETE FROM sessions")
            return {"type": "http.request", "body": raw[3:], "more_body": False}
        async def send(message):
            messages.append(message)
        path = "/api/file-imports/" + first["upload_id"] + "/chunks"
        headers = [(b"host", b"testserver"),
            (b"cookie", ("rd_session=" + b["owner"].cookie).encode()),
            (b"x-rd-csrf", b["owner"].csrf.encode()),
            (b"x-codepier-space", b"team"),
            (b"x-chunk-sha256", hashlib.sha256(raw).hexdigest().encode())]
        scope = {"type": "http", "asgi": {"version": "3.0"}, "method": "PUT",
                 "scheme": "http", "server": ("testserver", 80), "client": ("testclient", 1234),
                 "path": path, "raw_path": path.encode(), "query_string": b"offset=0",
                 "headers": headers, "http_version": "1.1", "root_path": ""}
        await mini(scope, receive, send)
        return messages
    messages = asyncio.run(scenario())
    assert next(m["status"] for m in messages if m["type"] == "http.response.start") == 401
    assert len(calls) == n


def test_real_runtime_admission_encrypts_bytes_and_redacts_summaries(ingress):
    import json
    app, b, _, calls, _, _ = ingress
    raw = b"opaque-no-public-summary-contents\x00\xff"
    _, first = begin(b["owner"], raw)
    b["owner"].put("/api/file-imports/" + first["upload_id"] + "/chunks?offset=0",
                   content=raw, headers={"X-Chunk-Sha256": hashlib.sha256(raw).hexdigest()})
    name, args, principal = calls[-1]
    runtime = app.state.runtime
    project = runtime.project("project-team", principal)
    identifier, _ = runtime._admit_operation(name, args, project, principal)
    operation = app.state.store.one("SELECT * FROM operations WHERE id=?", (identifier,))
    encoded = base64.b64encode(raw).decode()
    assert encoded not in operation["args_summary"]
    assert encoded not in operation["payload"]
    assert json.loads(app.state.store.decrypt(operation["payload"]))["args"]["data"] == encoded
    assert all(encoded not in row["detail"] for row in app.state.store.all("SELECT detail FROM audit"))


def persisted_failure(runtime, monkeypatch, code, *, operation_state="failed", throw=True):
    """A real durable Runtime record, with only remote execution simulated."""
    import json
    async def invoke(name, args, principal):
        def admit_and_finish():
            project = runtime.project(args["project"], principal)
            identifier, previous = runtime._admit_operation(name, args, project, principal)
            result = previous or {"ok": False, "error": {"code": code, "message": "Synthetic node denial"}}
            if previous is None:
                runtime.store.execute("UPDATE operations SET state=?,result=?,updated=? WHERE id=?",
                    (operation_state, json.dumps(result), time.time(), identifier))
            if throw:
                raise DevError(code, "Synthetic node denial", 409, operation_id=identifier)
            return {"operation_id": identifier, "pending": True, "state": operation_state}
        return await runtime.store.run(admit_and_finish)
    monkeypatch.setattr(runtime, "invoke", invoke)


@pytest.mark.parametrize("code", ["FILE_IMPORT_DISABLED", "PROTECTED_PATH", "WORKSPACE_NOT_FOUND"])
def test_definite_failed_begins_release_active_quota_and_replay_failure(ingress, monkeypatch, code):
    import hub.incoming_files as module
    app, b, service, _, _, _ = ingress
    monkeypatch.setattr(module, "MAX_ACTIVE", 1)
    persisted_failure(app.state.runtime, monkeypatch, code)
    original = metadata()
    first = b["owner"].post("/api/file-imports", json=original)
    assert first.status_code == 409 and code in first.text
    row = app.state.store.one("SELECT * FROM incoming_file_imports")
    operation = app.state.store.one("SELECT * FROM operations WHERE idem=?", (original["idempotency_key"],))
    assert row["state"] == "failed" and row["upload_id"] == operation["id"]
    assert time.time() + module.RETENTION - 10 < row["expires"] < time.time() + module.RETENTION + 10
    for _ in range(9):
        response = b["owner"].post("/api/file-imports", json=metadata())
        assert response.status_code == 409 and code in response.text
    repeated = b["owner"].post("/api/file-imports", json=original)
    assert repeated.status_code == 409 and code in repeated.text
    assert app.state.store.one("SELECT COUNT(*) AS n FROM operations WHERE idem=?",
                              (original["idempotency_key"],))["n"] == 1
    saved = app.state.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
    assert saved["upload_id"] == row["upload_id"] and saved["expires"] == row["expires"]
    assert service.cleanup()["removed"] == 0


@pytest.mark.parametrize("state,code,expected", [
    ("queued", "FILE_IMPORT_DISABLED", "reserved"),
    ("running", "PROTECTED_PATH", "reserved"),
    ("reconnecting", "UPLOAD_STORAGE", "reserved"),
    ("unknown", "REMOTE_ERROR", "recovery_required"),
    ("needs_review", "INTERRUPTED", "recovery_required"),
    ("failed", "UPLOAD_RECOVERY_REQUIRED", "recovery_required"),
    ("failed", "UPLOAD_STORAGE", "recovery_required"),
    ("failed", "UNKNOWN_ERROR", "recovery_required"),
])
def test_nondefinitive_failure_never_frees_capacity(ingress, monkeypatch, state, code, expected):
    import hub.incoming_files as module
    app, b, _, _, _, _ = ingress
    monkeypatch.setattr(module, "MAX_ACTIVE", 1)
    persisted_failure(app.state.runtime, monkeypatch, code, operation_state=state)
    assert b["owner"].post("/api/file-imports", json=metadata()).status_code == 409
    assert app.state.store.one("SELECT state FROM incoming_file_imports")["state"] == expected
    assert b["owner"].post("/api/file-imports", json=metadata()).status_code == 429


@pytest.mark.parametrize("method,code,expected", [
    ("status", "UPLOAD_INTEGRITY", "failed"),
    ("status", "UPLOAD_EXPIRED", "failed"),
    ("chunk", "UPLOAD_OFFSET", "reserved"),
    ("finish", "UPLOAD_INCOMPLETE", "reserved"),
    ("finish", "UPLOAD_INTEGRITY", "recovery_required"),
    ("finish", "UPLOAD_RECOVERY_REQUIRED", "recovery_required"),
])
def test_terminal_step_is_distinguished_from_resumable_or_ambiguous(ingress, monkeypatch, method, code, expected):
    app, b, _, _, _, _ = ingress
    _, first = begin(b["owner"], b"x")
    persisted_failure(app.state.runtime, monkeypatch, code)
    path = "/api/file-imports/" + first["upload_id"]
    if method == "status":
        response = b["owner"].get(path)
    elif method == "chunk":
        response = b["owner"].put(path + "/chunks?offset=0", content=b"x",
                                  headers={"X-Chunk-Sha256": hashlib.sha256(b"x").hexdigest()})
    else:
        response = b["owner"].post(path + "/finish")
    assert response.status_code == 409
    assert app.state.store.one("SELECT state FROM incoming_file_imports")["state"] == expected


def test_late_persisted_begin_failure_reconciles_before_next_quota_check(ingress, monkeypatch):
    import json
    import hub.incoming_files as module
    app, b, _, _, _, _ = ingress
    monkeypatch.setattr(module, "MAX_ACTIVE", 1)
    persisted_failure(app.state.runtime, monkeypatch, "FILE_IMPORT_DISABLED", operation_state="queued", throw=False)
    first = b["owner"].post("/api/file-imports", json=metadata())
    assert first.status_code == 200 and first.json()["pending"]
    row = app.state.store.one("SELECT * FROM incoming_file_imports")
    assert row["state"] == "reserved"
    app.state.store.execute("UPDATE operations SET state='failed',result=? WHERE id=?",
        (json.dumps({"ok": False, "error": {"code": "FILE_IMPORT_DISABLED", "message": "disabled"}}), row["upload_id"]))
    second = b["owner"].post("/api/file-imports", json=metadata())
    assert second.status_code == 200
    assert app.state.store.one("SELECT state FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))["state"] == "failed"


def test_cleanup_has_bounded_seven_day_retention_and_preserves_operations(ingress, monkeypatch):
    import hub.incoming_files as module
    app, b, service, _, _, _ = ingress
    persisted_failure(app.state.runtime, monkeypatch, "PROTECTED_PATH")
    for _ in range(4):
        assert b["owner"].post("/api/file-imports", json=metadata()).status_code == 409
    rows = app.state.store.all("SELECT * FROM incoming_file_imports ORDER BY request_key")
    future = time.time() + 50
    app.state.store.execute("UPDATE incoming_file_imports SET expires=?", (future,))
    assert service.cleanup()["removed"] == 0
    for row in rows[:3]:
        app.state.store.execute("UPDATE incoming_file_imports SET expires=? WHERE request_key=?",
                                (time.time() - 1, row["request_key"]))
    monkeypatch.setattr(module, "CLEANUP_BATCH", 2)
    assert service.cleanup()["removed"] == 2
    assert app.state.store.one("SELECT COUNT(*) AS n FROM incoming_file_imports")["n"] == 2
    assert service.cleanup()["removed"] == 1
    assert app.state.store.one("SELECT request_key FROM incoming_file_imports")["request_key"] == rows[3]["request_key"]
    assert app.state.store.one("SELECT COUNT(*) AS n FROM operations")["n"] == 4


def test_expired_unknown_and_ambiguous_records_survive_cleanup_and_keep_quota(ingress, monkeypatch):
    import hub.incoming_files as module
    app, b, service, _, _, _ = ingress
    monkeypatch.setattr(module, "MAX_ACTIVE", 1)
    persisted_failure(app.state.runtime, monkeypatch, "UPLOAD_RECOVERY_REQUIRED")
    assert b["owner"].post("/api/file-imports", json=metadata()).status_code == 409
    app.state.store.execute("UPDATE incoming_file_imports SET expires=?", (time.time() - module.RETENTION * 2,))
    assert service.cleanup()["removed"] == 0
    assert app.state.store.one("SELECT state FROM incoming_file_imports")["state"] == "recovery_required"
    assert b["owner"].post("/api/file-imports", json=metadata()).status_code == 429


def test_expired_unfinished_pending_is_not_forgotten(ingress, monkeypatch):
    import hub.incoming_files as module
    app, b, service, _, _, _ = ingress
    persisted_failure(app.state.runtime, monkeypatch, "REMOTE_ERROR", operation_state="queued", throw=False)
    assert b["owner"].post("/api/file-imports", json=metadata()).status_code == 200
    app.state.store.execute("UPDATE incoming_file_imports SET expires=?", (time.time() - module.RETENTION - 1,))
    assert service.cleanup() == {"removed": 0, "retained": 1}
    assert app.state.store.one("SELECT state FROM incoming_file_imports")["state"] == "recovery_required"


def test_expired_unadmitted_reservation_is_retained_seven_days_then_pruned(ingress):
    import hub.incoming_files as module
    app, b, service, _, _, _ = ingress
    _, first = begin(b["owner"])
    # The fixture Agent double created no operation, representing a reservation
    # whose HTTP waiter disconnected before Runtime admission.
    app.state.store.execute("UPDATE incoming_file_imports SET expires=?", (time.time() - 10,))
    assert service.cleanup()["removed"] == 0
    app.state.store.execute("UPDATE incoming_file_imports SET expires=?", (time.time() - module.RETENTION - 10,))
    assert service.cleanup()["removed"] == 1
    assert not app.state.store.one("SELECT * FROM incoming_file_imports WHERE upload_id=?", (first["upload_id"],))


def lost_finish_receipt(ingress, raw=b"", headers=None):
    """Persist successful Agent completion without running HTTP reconciliation."""
    import json
    from hub.incoming_files import _step_key
    app, b, service, calls, _, uploads = ingress
    if headers is None:
        _, first = begin(b["owner"], raw)
    else:
        response = b["owner"].post("/api/file-imports", json=metadata(raw), headers=headers)
        assert response.status_code == 200, response.text
        first = response.json()
    principal = calls[-1][2]
    args = {"project": "project-team", "workspace_id": "", "upload_id": first["upload_id"],
            "idempotency_key": _step_key(first["upload_id"], "finish")}
    runtime = app.state.runtime
    project = runtime.project("project-team", principal)
    identifier, _ = runtime._admit_operation("incoming_upload_finish", args, project, principal)
    completed = {**uploads[first["upload_id"]], "state": "complete", "ready": True, "created": True,
                 "expires": time.time() + 7 * 86400, "received": len(raw)}
    uploads[first["upload_id"]] = completed
    app.state.store.execute("UPDATE operations SET state='succeeded',result=?,updated=? WHERE id=?",
        (json.dumps({"ok": True, "data": completed}), time.time(), identifier))
    row = app.state.store.one("SELECT * FROM incoming_file_imports WHERE upload_id=?", (first["upload_id"],))
    assert row["state"] == "reserved" and row["expires"] < completed["expires"]
    return identifier, principal, row, completed


@pytest.mark.parametrize("recovery_path", ["operation", "status", "shared_check"])
def test_lost_finish_reply_recovers_past_begin_ttl_under_live_authority(ingress, monkeypatch, recovery_path):
    from types import SimpleNamespace
    import hub.incoming_files as module
    app, b, service, _, _, _ = ingress
    identifier, principal, row, completed = lost_finish_receipt(ingress)
    # Advance only the ingress clock: account credentials remain independently
    # live. No production clock or authentication record is changed.
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: row["expires"] + 1))
    if recovery_path == "operation":
        response = b["owner"].get("/api/operations/" + identifier)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["data"]["state"] == "complete"
    elif recovery_path == "status":
        response = b["owner"].get("/api/file-imports/" + row["upload_id"])
        assert response.status_code == 200 and response.json()["state"] == "complete", response.text
    else:
        checked, _, _ = service._check(row, principal)
        assert checked["state"] == "finished"
    saved = app.state.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
    assert saved["state"] == "finished"
    assert saved["expires"] == completed["expires"]


@pytest.mark.parametrize("change", [
    {"state": "receiving"}, {"created": False}, {"ready": False},
    {"upload_id": "a" * 32}, {"path": "unrelated"}, {"bytes": 1}, {"bytes": False},
    {"received": 1}, {"received": False}, {"sha256": "f" * 64},
    {"expires": float("inf")}, {"expires": True}, {"expires": 1},
])
def test_expired_manifest_never_adopts_invalid_saved_finish_fields(ingress, monkeypatch, change):
    import json
    from types import SimpleNamespace
    import hub.incoming_files as module
    app, _, service, _, _, _ = ingress
    identifier, principal, row, completed = lost_finish_receipt(ingress)
    app.state.store.execute("UPDATE operations SET result=? WHERE id=?",
        (json.dumps({"ok": True, "data": {**completed, **change}}), identifier))
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: row["expires"] + 1))
    with pytest.raises(DevError, match="expired"):
        service._check(row, principal)
    assert app.state.store.one("SELECT state FROM incoming_file_imports")["state"] == "reserved"


@pytest.mark.parametrize("tamper", ["pending", "wrong_grant", "wrong_workspace", "too_long", "expired_receipt"])
def test_saved_completion_requires_exact_binding_finality_and_agent_retention(ingress, monkeypatch, tamper):
    import json
    from types import SimpleNamespace
    import hub.incoming_files as module
    app, _, service, _, _, _ = ingress
    identifier, principal, row, completed = lost_finish_receipt(ingress)
    if tamper == "pending":
        app.state.store.execute("UPDATE operations SET state='queued' WHERE id=?", (identifier,))
    elif tamper == "wrong_grant":
        app.state.store.execute("UPDATE operations SET grant_id='other-grant' WHERE id=?", (identifier,))
    elif tamper == "wrong_workspace":
        operation = app.state.store.one("SELECT args_summary FROM operations WHERE id=?", (identifier,))
        summary = {**json.loads(operation["args_summary"]), "workspace_id": "a" * 32}
        app.state.store.execute("UPDATE operations SET args_summary=? WHERE id=?", (json.dumps(summary), identifier))
    else:
        completed["expires"] = time.time() + 8 * 86400 if tamper == "too_long" else row["expires"]
        app.state.store.execute("UPDATE operations SET result=? WHERE id=?",
            (json.dumps({"ok": True, "data": completed}), identifier))
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: row["expires"] + 1))
    with pytest.raises(DevError, match="expired"):
        service._check(row, principal)
    assert app.state.store.one("SELECT state FROM incoming_file_imports")["state"] == "reserved"


@pytest.mark.parametrize("denial", ["read_only", "remapped", "revoked"])
def test_saved_finish_does_not_bypass_current_authority_or_mapping(ingress, monkeypatch, denial):
    from types import SimpleNamespace
    import hub.incoming_files as module
    app, b, _, _, _, _ = ingress
    identifier, _, row, _ = lost_finish_receipt(ingress)
    if denial == "read_only":
        app.state.store.execute("UPDATE projects SET mode='read' WHERE id='project-team'")
    elif denial == "remapped":
        app.state.store.execute("UPDATE projects SET root=root||'/changed' WHERE id='project-team'")
    else:
        app.state.store.execute("DELETE FROM sessions")
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: row["expires"] + 1))
    response = b["owner"].get("/api/operations/" + identifier)
    assert response.status_code in (401, 403, 409)
    assert app.state.store.one("SELECT state FROM incoming_file_imports")["state"] == "reserved"


@pytest.mark.parametrize("step", ["begin", "chunk", "status"])
@pytest.mark.parametrize("after_begin_expiry", [False, True])
@pytest.mark.parametrize("stale_state", ["receiving", "queued", "complete"])
def test_stale_step_receipts_cannot_regress_completed_metadata(ingress, monkeypatch, step, after_begin_expiry, stale_state):
    from types import SimpleNamespace
    import hub.incoming_files as module
    app, _, service, _, _, _ = ingress
    finish_id, principal, row, completed = lost_finish_receipt(ingress, b"abc")
    service._check(row, principal)
    if after_begin_expiry:
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: row["expires"] + 1))
    requested_id = row["upload_id"] if step == "begin" else uuid.uuid4().hex
    stale = {**completed, "operation_id": requested_id, "state": "receiving",
             "received": 0, "ready": False, "created": False, "expires": row["expires"]}
    if stale_state == "complete":
        stale.update(state="complete", received=row["bytes"], ready=True, created=True)
    elif stale_state == "queued":
        stale.update(state="queued", pending=True)
    reply = service.reconcile_for(principal, row, stale, begin=step == "begin")
    assert reply["operation_id"] == requested_id
    assert reply["completion_operation_id"] == finish_id
    for key in ("state", "received", "ready", "created", "expires"):
        assert reply[key] == completed[key]
    saved = app.state.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
    assert saved["state"] == "finished" and saved["expires"] == completed["expires"]


def test_stale_begin_replay_then_local_uploader_retry_after_24_hours(ingress, monkeypatch, tmp_path):
    from types import SimpleNamespace
    import hub.incoming_files as module
    import scripts.file_import_client as local
    app, b, service, _, _, _ = ingress
    grant = b["owner"].post("/api/grants", json={"label": "local replay fixture",
        "scopes": ["read", "write"], "projects": ["project-team"], "days": 2})
    assert grant.status_code == 200, grant.text
    headers = {"Authorization": "Bearer " + grant.json()["token"]}
    finish_id, principal, row, completed = lost_finish_receipt(ingress, b"abc", headers=headers)
    stale = {**completed, "operation_id": row["upload_id"], "state": "receiving",
             "received": 0, "ready": False, "created": False, "expires": row["expires"]}
    runtime = app.state.runtime
    original = runtime.invoke
    async def cached_begin(name, args, caller):
        result = await original(name, args, caller)
        return stale if name == "incoming_upload_begin" else result
    monkeypatch.setattr(runtime, "invoke", cached_begin)
    # A replay within the first day must not silently shorten the saved expiry.
    first_replay = service.reconcile_for(principal, row, stale, begin=True)
    assert first_replay["state"] == "complete" and first_replay["completion_operation_id"] == finish_id
    future = row["expires"] + 1
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: future))
    monkeypatch.setattr(local, "time", SimpleNamespace(time=lambda: future, monotonic=time.monotonic, sleep=time.sleep))
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    receipt = local.upload_local_file(b["owner"].client, "http://testserver", headers,
        source=source, project=row["project_id"], destination=row["path"],
        idempotency_key=row["begin_key"], allowed_roots=[tmp_path], retry_seconds=2)
    assert receipt["created"] is True and receipt["state"] == "complete"
    assert receipt["expires"] == completed["expires"]
    saved = app.state.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
    assert saved["expires"] == completed["expires"]


def test_completed_manifest_never_fabricates_receipt_if_canonical_finish_is_corrupt(ingress):
    import json
    app, _, service, _, _, _ = ingress
    identifier, principal, row, completed = lost_finish_receipt(ingress)
    service._check(row, principal)
    app.state.store.execute("UPDATE operations SET result=? WHERE id=?",
        (json.dumps({"ok": True, "data": {**completed, "sha256": "f" * 64}}), identifier))
    stale = {**completed, "operation_id": row["upload_id"], "state": "receiving",
             "ready": False, "created": False, "expires": row["expires"]}
    with pytest.raises(DevError) as exc:
        service.reconcile_for(principal, row, stale, begin=True)
    assert exc.value.code == "INVALID_IMPORT_RECEIPT"
    saved = app.state.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
    assert saved["state"] == "finished" and saved["expires"] == completed["expires"]
