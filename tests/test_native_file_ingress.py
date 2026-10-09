"""Synthetic native-file relay contracts, including the real Hub/Agent byte bridge.

No provider, credential, deployment gate or network is enabled outside monkeypatch.
Authentication itself is covered by HTTP ingress tests; this fixture deliberately
uses a small live-authority Runtime double and real temporary Store/Journal.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import os
import stat
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from agent import incoming_artifacts
from agent.filesystem import FileEngine
from agent.incoming_uploads import IncomingUploads
from agent.journal import Journal
from hub import native_file_ingress as native
from hub.incoming_files import IncomingFileService
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from shared.file_sources import DEFAULT_FILE_HOSTS
from shared.util import DevError


SOURCE_ID = "synthetic-native-file-PRIVATE-ID"
SOURCE_TICKET = "PRIVATE-SIGNED-TICKET"
SOURCE_URL = "https://files.oaiusercontent.com/private-object?sig=" + SOURCE_TICKET


def sha(data):
    return hashlib.sha256(data).hexdigest()


def request(data=b"abc", **changes):
    return {
        "project": "project-a", "workspace_id": "",
        "idempotency_key": "native-file-fixture-key", "path": "资料/附件 🧪.opaque",
        "file": {"file_id": SOURCE_ID, "download_url": SOURCE_URL, "size": len(data)},
        "expected_sha256": sha(data), **changes,
    }


def inspect_request(**changes):
    value = request(**changes)
    value.pop("path")
    value.pop("expected_sha256")
    return value


def expect_error(code, awaitable):
    with pytest.raises(DevError) as error:
        asyncio.run(awaitable)
    assert error.value.code == code
    return error.value


class BridgeRuntime:
    """Only dispatch/identity is synthetic; ingress, SQLite and bytes are real."""

    def __init__(self, store, uploads, root):
        self.store, self.uploads, self.root = store, uploads, root
        self.mapping = {"id": "project-a", "device_id": "node-a", "root": str(root),
                        "mode": "write", "device_enabled": True}
        self.allowed = True
        self.connected = True
        self.calls, self.operations, self.keys, self.waits = [], {}, {}, {}
        self.pending_tools = set()
        self.failed_tools = set()
        self.receipt_changes = {}
        self.finish_context = {}
        self.incoming_files = IncomingFileService(self)

    def online(self, device_id):
        return self.connected

    def project(self, value, principal):
        self.authorize(principal, "read", project_id=value)
        if value != self.mapping["id"]:
            raise DevError("PROJECT_NOT_FOUND", "Synthetic project not found", 404)
        return dict(self.mapping)

    def authorize(self, principal, scope, *, project_id=None):
        if not self.allowed:
            raise DevError("INVALID_TOKEN", "Synthetic grant revoked", 401)
        if scope not in principal.scopes:
            raise DevError("INSUFFICIENT_SCOPE", "Synthetic scope denied", 403)
        if (principal.user_id != "owner-a" or principal.space_id != "legacy"
                or project_id and project_id not in principal.projects):
            raise DevError("PROJECT_NOT_FOUND", "Synthetic project unavailable", 404)

    async def invoke(self, name, args, principal):
        args = dict(args)
        service = self.incoming_files
        current = self.project(args["project"], principal)
        await self.store.run(service.validate_admission, name, args, current, principal)
        key = args.get("idempotency_key")
        operation = self.keys.setdefault((principal.actor, key), uuid.uuid4().hex) if key else uuid.uuid4().hex
        self.calls.append((name, args, operation))
        project = {**current, "_coding_owner": principal.user_id,
                   "_coding_device": current["device_id"], "_coding_scopes": sorted(principal.scopes),
                   "_workspace_id": args.get("workspace_id", "")}
        if name == "incoming_upload_begin":
            receipt = self.uploads.begin(operation, project, args)
        elif name == "incoming_upload_chunk":
            receipt = self.uploads.chunk(project, {**args, "data": base64.b64decode(args["data"])})
        else:
            method = self.uploads.status if name == "incoming_upload_status" else self.uploads.finish
            receipt = method(project, args)
        if name == "incoming_upload_finish":
            receipt = {**receipt, **self.receipt_changes}
            self.finish_context[operation] = (dict(args), principal)
            self.persist_finish(operation, args, receipt, principal,
                                state="running" if name in self.pending_tools else "succeeded")
        self.operations[operation] = (name, dict(receipt))
        if name in self.pending_tools:
            return {"operation_id": operation, "pending": True, "state": "queued"}
        # The real service must remove unexpected node fields before public use.
        return {**receipt, "operation_id": operation,
                "download_url": SOURCE_URL, "file_id": SOURCE_ID, "ticket": SOURCE_TICKET,
                "output": "untrusted node output", "data": "untrusted node payload"}

    def persist_finish(self, identifier, args, receipt, principal, *, state="succeeded"):
        self.store.execute(
            "INSERT INTO operations(id,actor,grant_id,tool,args_summary,fingerprint,idem,state,result,"
            "created,updated,space_id,owner_user_id,project_id,device_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state,result=excluded.result,updated=excluded.updated",
            (identifier, principal.actor, principal.grant_id, "incoming_upload_finish", json.dumps(args),
             "synthetic", args["idempotency_key"], state, json.dumps({"ok": True, "data": receipt}),
             time.time(), time.time(), principal.space_id, principal.user_id,
             self.mapping["id"], self.mapping["device_id"]))

    def authorized_result(self, identifier, result, principal):
        self.authorize(principal, "write", project_id=self.mapping["id"])
        operation = self.store.one("SELECT * FROM operations WHERE id=?", (identifier,))
        assert operation is not None
        assert (operation["actor"], operation["grant_id"], operation["owner_user_id"], operation["space_id"]) == (
            principal.actor, principal.grant_id, principal.user_id, principal.space_id)
        self.incoming_files.authorize_operation(operation, principal)
        return Runtime.unwrap(identifier, result)

    async def wait_operation(self, identifier, principal, seconds):
        self.authorize(principal, "read")
        self.waits[identifier] = self.waits.get(identifier, 0) + 1
        await asyncio.sleep(0)

    def operation(self, identifier, principal, options):
        self.authorize(principal, "read")
        assert options == {"include_output": False}
        name, data = self.operations[identifier]
        if self.waits.get(identifier, 0) < 2:
            return {"pending": True}
        if name in self.failed_tools:
            return {"pending": False, "result": {"ok": False, "error": {"code": "SYNTHETIC_FAILURE"}}}
        if name == "incoming_upload_finish":
            args, original_principal = self.finish_context[identifier]
            self.persist_finish(identifier, args, data, original_principal)
        return {"pending": False, "result": {"ok": True, "data": data}}


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPIER_FILE_IMPORT_STREAMING", "true")
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_RELAY", "true")
    monkeypatch.delenv("CODEPIER_NATIVE_FILE_HOSTS", raising=False)
    monkeypatch.delenv("CODEPIER_NATIVE_FILE_PROVIDERS", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    config_path = tmp_path / "synthetic-config.json"
    config_path.write_text("{}")
    journal = Journal(tmp_path / "agent-state")
    engine = FileEngine({"allowed_roots": [{"path": str(root), "writable": True}], "tasks": {}},
                        journal, config_path)
    store = Store(tmp_path / "hub-state")
    store.iam_enabled = False
    store.execute("INSERT INTO users(id,username,password_hash,created) VALUES(?,?,?,?)",
                  ("owner-a", "synthetic-owner", "not-a-password", time.time()))
    store.execute("INSERT INTO devices(id,name,secret,info,created) VALUES(?,?,?,?,?)",
                  ("node-a", "synthetic", "unused", json.dumps({"capabilities": ["incoming_upload_begin"]}), time.time()))
    runtime = BridgeRuntime(store, IncomingUploads(engine), root)
    principal = Principal("fixture-owner", "owner-a", {"read", "write"}, ["project-a"])
    state = SimpleNamespace(runtime=runtime, principal=principal, payload=b"abc", downloads=[],
                            temporary_files=[], root=root, store=store, engine=engine)
    original_temporary = native.tempfile.TemporaryFile

    def temporary(*args, **kwargs):
        result = original_temporary(*args, **kwargs)
        state.temporary_files.append(result)
        return result

    def download(file, hosts, max_bytes, *, providers=()):
        state.downloads.append((dict(file), tuple(hosts), max_bytes, tuple(providers)))
        for offset in range(0, len(state.payload), 17311):
            yield state.payload[offset:offset + 17311]

    monkeypatch.setattr(native.tempfile, "TemporaryFile", temporary)
    monkeypatch.setattr(native, "download_chunks", download)
    yield state
    assert all(file.closed for file in state.temporary_files)
    assert getattr(runtime, "_native_ingress_active", 0) == 0
    journal.db.close()
    store.close()


def run_import(relay, value=None, principal=None):
    return asyncio.run(native.import_native_file(relay.runtime, value or request(relay.payload),
                                                 principal or relay.principal))


@pytest.mark.parametrize("data,path", [
    (b"", "empty.bin"),
    (bytes(range(256)) * 2051, "资料/附件 🧪.opaque"),
    ("Unicode 内容🙂".encode(), "文本.unknown"),
    (b"#!/bin/sh\nexit 99\n", "never-execute.exe"),
])
def test_real_hub_agent_roundtrip_is_exact_and_private(relay, caplog, data, path):
    relay.payload = data
    result = run_import(relay, request(data, path=path))
    assert (relay.root / path).read_bytes() == data
    assert result["created"] is result["ready"] is True
    assert result["state"] == "complete"
    assert result["bytes"] == len(data) and result["sha256"] == sha(data)
    assert result["transfer"] == "authenticated_hub_ingress"
    assert not result["executed"] and not result["extracted"] and not result["overwritten"]
    if os.name != "nt":
        assert not stat.S_IMODE((relay.root / path).stat().st_mode) & 0o111
    serialized = json.dumps(result) + caplog.text
    saved = json.dumps(relay.store.all("SELECT * FROM incoming_file_imports"))
    dispatch = json.dumps(relay.runtime.calls)
    for private in (SOURCE_ID, SOURCE_TICKET, SOURCE_URL, "untrusted node output", "untrusted node payload"):
        assert private not in serialized
        assert private not in saved
        assert private not in dispatch
    chunks = [args for name, args, _ in relay.runtime.calls if name == "incoming_upload_chunk"]
    assert b"".join(base64.b64decode(args["data"]) for args in chunks) == data
    assert all(len(base64.b64decode(args["data"])) <= native.CHUNK for args in chunks)
    assert all(args["chunk_sha256"] == sha(base64.b64decode(args["data"])) for args in chunks)
    assert not data or chunks
    assert not list(relay.root.rglob(".rd-import-*"))


@pytest.mark.parametrize("metadata", [{}, {"size": None}, {"mime_type": None, "file_name": None, "name": None}])
def test_missing_optional_metadata_computes_exact_bytes(relay, metadata):
    value = request()
    value["file"] = {"file_id": SOURCE_ID, "download_url": SOURCE_URL, **metadata}
    value.pop("expected_sha256")
    result = run_import(relay, value)
    assert result["bytes"] == 3 and result["sha256"] == sha(b"abc")


@pytest.mark.parametrize("change,code", [
    ({"size": 0}, "ARTIFACT_SIZE"), ({"size": 4}, "ARTIFACT_SIZE"),
    ({"expected_sha256": "0" * 64}, "ARTIFACT_INTEGRITY"),
])
def test_metadata_mismatch_never_begins_or_publishes(relay, change, code):
    value = request()
    if "size" in change:
        value["file"].update(change)
    else:
        value.update(change)
    expect_error(code, native.import_native_file(relay.runtime, value, relay.principal))
    assert not relay.runtime.calls
    assert not relay.store.all("SELECT * FROM incoming_file_imports")
    assert not list(relay.root.iterdir())


def test_changed_signed_url_same_key_and_bytes_reuses_same_upload(relay):
    first = run_import(relay)
    value = request()
    value["file"]["download_url"] = "https://cdn.openai.com/new-object?sig=refreshed-private-ticket"
    value["file"]["file_id"] = "refreshed-private-file-id"
    second = run_import(relay, value)
    assert first["upload_id"] == second["upload_id"]
    assert len(relay.store.all("SELECT * FROM incoming_file_imports")) == 1
    mutations = [(name, op) for name, _, op in relay.runtime.calls if name != "incoming_upload_status"]
    assert len({op for name, op in mutations if name == "incoming_upload_begin"}) == 1
    assert len([name for name, _ in mutations if name == "incoming_upload_chunk"]) == 1
    assert len([name for name, _ in mutations if name == "incoming_upload_finish"]) == 1
    assert (relay.root / value["path"]).read_bytes() == b"abc"


@pytest.mark.parametrize("changed", [{"path": "different.bin"}, {"workspace_id": "a" * 32}])
def test_same_key_cannot_retarget_existing_import(relay, changed):
    run_import(relay)
    before = len(relay.runtime.calls)
    expect_error("IDEMPOTENCY_CONFLICT",
                 native.import_native_file(relay.runtime, request(**changed), relay.principal))
    assert len(relay.runtime.calls) == before
    assert len(relay.store.all("SELECT * FROM incoming_file_imports")) == 1


@pytest.mark.parametrize("principal", [
    Principal("other", "another-owner", {"read", "write"}, ["project-a"]),
    Principal("fixture-owner", "owner-a", {"read", "write"}, ["project-a"], space_id="another-space"),
    Principal("fixture-owner", "owner-a", {"read"}, ["project-a"]),
    Principal("fixture-owner", "owner-a", {"read", "write"}, ["project-b"]),
])
def test_current_authority_rejects_before_fetch(relay, principal):
    with pytest.raises(DevError):
        run_import(relay, principal=principal)
    assert not relay.downloads and not relay.runtime.calls


def test_cross_project_selector_never_fetches(relay):
    expect_error("PROJECT_NOT_FOUND",
                 native.import_native_file(relay.runtime, request(project="project-b"), relay.principal))
    assert not relay.downloads


@pytest.mark.parametrize("change,code", [
    ("offline", "FILE_IMPORT_DEVICE_OFFLINE"), ("old", "AGENT_UPGRADE_REQUIRED"),
    ("bad-json", "AGENT_UPGRADE_REQUIRED"), ("bad-capabilities", "AGENT_UPGRADE_REQUIRED"),
])
def test_unavailable_node_never_fetches_source(relay, change, code):
    if change == "offline":
        relay.runtime.connected = False
    else:
        info = {"old": "{}", "bad-json": "{", "bad-capabilities": '{"capabilities":"incoming_upload_begin"}'}[change]
        relay.store.execute("UPDATE devices SET info=?", (info,))
    expect_error(code, native.import_native_file(relay.runtime, request(), relay.principal))
    assert not relay.downloads and not relay.runtime.calls


@pytest.mark.parametrize("change,code", [
    ("revoke", "INVALID_TOKEN"), ("root", "FILE_IMPORT_MAPPING_CHANGED"),
    ("device", "FILE_IMPORT_MAPPING_CHANGED"), ("read-only", "PROJECT_READ_ONLY"),
    ("offline", "FILE_IMPORT_DEVICE_OFFLINE"),
])
def test_authority_and_binding_refresh_after_download(relay, monkeypatch, change, code):
    def download(*args, **kwargs):
        yield b"a"
        if change == "revoke":
            relay.runtime.allowed = False
        elif change == "read-only":
            relay.runtime.mapping["mode"] = "read"
        elif change == "offline":
            relay.runtime.connected = False
        else:
            relay.runtime.mapping[change if change == "root" else "device_id"] += "-retargeted"
            if change == "device":
                relay.store.execute("UPDATE devices SET id=?", (relay.runtime.mapping["device_id"],))
        yield b"bc"
    monkeypatch.setattr(native, "download_chunks", download)
    expect_error(code, native.import_native_file(relay.runtime, request(), relay.principal))
    assert not relay.runtime.calls
    assert not list(relay.root.iterdir())


def test_policy_defaults_and_exact_explicit_restriction(relay, monkeypatch):
    policy = native.native_source_policy()
    assert policy["allowed_hosts"] == list(DEFAULT_FILE_HOSTS)
    assert policy["file_source_providers"] == []
    assert policy["policy_scope"] == "hub"
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_HOSTS", "[]")
    expect_error("ARTIFACT_SOURCE_DENIED",
                 native.import_native_file(relay.runtime, request(), relay.principal))
    assert not relay.downloads
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_HOSTS", '["download.owner-approved.invalid"]')
    value = request()
    value["file"]["download_url"] = "https://download.owner-approved.invalid/file"
    assert run_import(relay, value)["created"]
    assert relay.downloads[-1][1] == ("download.owner-approved.invalid",)


@pytest.mark.parametrize("setting,value", [
    ("CODEPIER_NATIVE_FILE_HOSTS", "not-json"),
    ("CODEPIER_NATIVE_FILE_HOSTS", '"files.oaiusercontent.com"'),
    ("CODEPIER_NATIVE_FILE_HOSTS", '["*.oaiusercontent.com"]'),
    ("CODEPIER_NATIVE_FILE_HOSTS", '["https://files.oaiusercontent.com"]'),
    ("CODEPIER_NATIVE_FILE_PROVIDERS", "null"),
    ("CODEPIER_NATIVE_FILE_PROVIDERS", '"openai_sediment"'),
    ("CODEPIER_NATIVE_FILE_PROVIDERS", '["openai"]'),
    ("CODEPIER_NATIVE_FILE_PROVIDERS", '["*"]'),
    ("CODEPIER_NATIVE_FILE_PROVIDERS", '["openai_sediment", 1]'),
])
def test_malformed_policy_fails_closed_before_source(relay, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    expect_error("FILE_IMPORT_POLICY_INVALID",
                 native.import_native_file(relay.runtime, request(), relay.principal))
    assert not relay.downloads and not relay.runtime.calls


@pytest.mark.parametrize("url", [
    "https://unapproved.invalid/private?sig=PRIVATE-SIGNED-TICKET",
    "https://sdmntprtest.oaiusercontent.com/private",
    "https://files.oaiusercontent.com.attacker.invalid/private",
    "https://unapproved.blob.core.windows.net/private",
    "https://unapproved.s3.amazonaws.com/private",
    "http://files.oaiusercontent.com/private",
    "https://user:password@files.oaiusercontent.com/private",
    "https://files.oaiusercontent.com:444/private",
    "https://127.0.0.1/private",
])
def test_unknown_or_unsafe_source_never_downloads(relay, caplog, url):
    value = request()
    value["file"]["download_url"] = url
    error = expect_error("ARTIFACT_SOURCE_DENIED",
                         native.import_native_file(relay.runtime, value, relay.principal))
    assert not relay.downloads and not relay.runtime.calls
    detail = str(error) + json.dumps(error.details) + caplog.text
    assert SOURCE_TICKET not in detail and SOURCE_ID not in detail
    assert "/private" not in detail and "password" not in detail


@pytest.mark.parametrize("url,allowed", [
    ("https://sdmntprtest.oaiusercontent.com/file", True),
    ("https://sdmntpr.oaiusercontent.com/file", False),
    ("https://child.sdmntprtest.oaiusercontent.com/file", False),
    ("https://sdmntprtest.oaiusercontent.com.evil.invalid/file", False),
    ("https://sdmntprtest.blob.core.windows.net/file", False),
    ("https://sdmntprtest.s3.amazonaws.com/file", False),
])
def test_named_provider_is_explicit_and_narrow(relay, monkeypatch, url, allowed):
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_PROVIDERS", '["openai_sediment"]')
    value = request()
    value["file"]["download_url"] = url
    if allowed:
        assert run_import(relay, value)["created"]
        assert relay.downloads[-1][3] == ("openai_sediment",)
    else:
        expect_error("ARTIFACT_SOURCE_DENIED",
                     native.import_native_file(relay.runtime, value, relay.principal))
        assert not relay.downloads


def test_provider_removed_between_requests_is_not_cached(relay, monkeypatch):
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_PROVIDERS", '["openai_sediment"]')
    value = request()
    value["file"]["download_url"] = "https://sdmntprtest.oaiusercontent.com/file"
    run_import(relay, value)
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_PROVIDERS", "[]")
    # A new transfer must consult the new policy; a completed retry needs no source.
    value["idempotency_key"] = "after-provider-removal"
    before = len(relay.downloads)
    expect_error("ARTIFACT_SOURCE_DENIED",
                 native.import_native_file(relay.runtime, value, relay.principal))
    assert len(relay.downloads) == before


def test_preflight_is_source_safe_and_never_downloads(relay, caplog):
    value = inspect_request()
    value["file"]["download_url"] = "https://unapproved.invalid/private?sig=" + SOURCE_TICKET
    result = asyncio.run(native.inspect_native_file(relay.runtime, value, relay.principal))
    assert result["checked"] and not result["source_allowed"] and result["approval_required"]
    assert result["source_host"] == "unapproved.invalid"
    assert result["approval_target"]["config_key"] == "CODEPIER_NATIVE_FILE_HOSTS"
    assert not result["request_sent"] and not result["created"]
    assert result["host_roundtrip"] == "not_run" and result["policy_scope"] == "hub"
    assert not relay.downloads and not relay.runtime.calls and not relay.temporary_files
    serialized = json.dumps(result) + caplog.text
    assert SOURCE_TICKET not in serialized and SOURCE_ID not in serialized and "/private" not in serialized


def test_declared_oversize_never_allocates_or_fetches(relay):
    value = request()
    value["file"]["size"] = native.native_source_policy()["max_bytes"] + 1
    expect_error("ARTIFACT_TOO_LARGE",
                 native.import_native_file(relay.runtime, value, relay.principal))
    assert not relay.downloads and not relay.temporary_files and not relay.runtime.calls


def test_service_quota_rejects_without_agent_dispatch(relay, monkeypatch):
    import hub.incoming_files as incoming
    monkeypatch.setattr(incoming, "MAX_ACTIVE", 0)
    expect_error("FILE_IMPORT_BUSY", native.import_native_file(relay.runtime, request(), relay.principal))
    assert not relay.runtime.calls
    assert not relay.store.all("SELECT * FROM incoming_file_imports")


def test_parallel_source_capacity_has_no_second_fetch(relay, monkeypatch):
    monkeypatch.setattr(native, "MAX_ACTIVE", 1)
    started, release = threading.Event(), threading.Event()
    def download(*args, **kwargs):
        started.set()
        assert release.wait(5)
        yield b"abc"
    monkeypatch.setattr(native, "download_chunks", download)
    async def scenario():
        first = asyncio.create_task(native.import_native_file(relay.runtime, request(), relay.principal))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            with pytest.raises(DevError) as error:
                await native.import_native_file(relay.runtime, request(idempotency_key="second-key"), relay.principal)
            assert error.value.code == "FILE_IMPORT_BUSY"
            assert len(relay.temporary_files) == 1
        finally:
            release.set()
            await first
    asyncio.run(scenario())


def test_download_exception_closes_anonymous_file(relay, monkeypatch):
    def download(*args, **kwargs):
        yield b"a"
        raise DevError("ARTIFACT_NETWORK", "Synthetic interrupted stream", 502)
    monkeypatch.setattr(native, "download_chunks", download)
    expect_error("ARTIFACT_NETWORK",
                 native.import_native_file(relay.runtime, request(), relay.principal))
    assert len(relay.temporary_files) == 1 and relay.temporary_files[0].closed
    assert not relay.runtime.calls


@pytest.mark.parametrize("cancel_count", [1, 2, 4])
def test_cancellation_drains_writer_before_temporary_close(relay, monkeypatch, cancel_count):
    started, release = threading.Event(), threading.Event()
    observed = []
    def download(*args, **kwargs):
        yield b"a"
        started.set()
        assert release.wait(5)
        observed.append(relay.temporary_files[0].closed)
        yield b"bc"
    monkeypatch.setattr(native, "download_chunks", download)
    async def scenario():
        task = asyncio.create_task(native.import_native_file(relay.runtime, request(), relay.principal))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
                assert not relay.temporary_files[0].closed
                assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert observed == [False]
    assert relay.temporary_files[0].closed and not relay.runtime.calls


def test_all_pending_steps_poll_original_operations_without_replay(relay):
    relay.runtime.pending_tools = {
        "incoming_upload_begin", "incoming_upload_status", "incoming_upload_chunk", "incoming_upload_finish",
    }
    result = run_import(relay)
    assert result["created"]
    assert len(relay.runtime.calls) == 4
    assert len(relay.runtime.waits) == 4 and set(relay.runtime.waits.values()) == {2}
    for name, args, identifier in relay.runtime.calls:
        assert identifier in relay.runtime.waits
        if name != "incoming_upload_status":
            assert args["idempotency_key"]
    assert len({identifier for _, _, identifier in relay.runtime.calls}) == 4


def test_pending_operation_failure_is_not_replayed(relay):
    relay.runtime.pending_tools = {"incoming_upload_begin"}
    relay.runtime.failed_tools = {"incoming_upload_begin"}
    error = expect_error("SYNTHETIC_FAILURE",
                         native.import_native_file(relay.runtime, request(), relay.principal))
    assert len(relay.runtime.calls) == 1
    assert error.details["operation_id"] == relay.runtime.calls[0][2]
    assert not list(relay.root.iterdir())


def test_pending_deadline_preserves_original_receipt(relay, monkeypatch):
    relay.runtime.pending_tools = {"incoming_upload_begin"}
    monkeypatch.setattr(native, "TRANSFER_SECONDS", 0)
    error = expect_error("FILE_IMPORT_PENDING",
                         native.import_native_file(relay.runtime, request(), relay.principal))
    assert len(relay.runtime.calls) == 1
    assert error.details["operation_id"] == relay.runtime.calls[0][2]
    assert error.details["upload_id"] == relay.runtime.calls[0][2]
    assert not relay.runtime.waits


@pytest.mark.parametrize("change", [
    {"created": False}, {"ready": False}, {"state": "receiving"}, {"bytes": 2},
    {"sha256": "0" * 64}, {"path": "another.bin"}, {"received": 2}, {"created": "true"},
])
def test_inconsistent_final_receipt_never_reports_success(relay, change):
    relay.runtime.receipt_changes = change
    expect_error("INVALID_IMPORT_RECEIPT",
                 native.import_native_file(relay.runtime, request(), relay.principal))


@pytest.mark.parametrize("streaming,relay_enabled", [
    ("false", "true"),
    ("true", "false"), ("TRUE", "true"), ("true", "TRUE"), ("1", "true"), ("true", " true"),
])
def test_dual_gate_preserves_explicit_disable_and_rejects_malformed(relay, monkeypatch, streaming, relay_enabled):
    for name, value in (("CODEPIER_FILE_IMPORT_STREAMING", streaming),
                        ("CODEPIER_NATIVE_FILE_RELAY", relay_enabled)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert not native.native_ingress_enabled()
    expect_error("FILE_IMPORT_DISABLED",
                 native.import_native_file(relay.runtime, request(), relay.principal))
    assert not relay.downloads


@pytest.mark.parametrize("name", ["download_artifact", "inspect_file_source"])
@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_always_uses_hub_adapter_and_never_silent_legacy_fallback(monkeypatch, name, enabled):
    monkeypatch.setenv("CODEPIER_FILE_IMPORT_STREAMING", "true")
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_RELAY", "true" if enabled else "false")
    calls = []
    async def adapter(runtime, raw, principal):
        calls.append(("relay", raw, principal))
        return {"route": "relay"}
    async def run(function, *args):
        return function(*args)
    def legacy(tool, raw, principal):
        calls.append(("legacy", raw, principal))
        return {"route": "legacy"}
    runtime = SimpleNamespace(_loop=None, store=SimpleNamespace(run=run, one=lambda *args: None), _invoke=legacy)
    monkeypatch.setattr(native, "import_native_file", adapter)
    monkeypatch.setattr(native, "inspect_native_file", adapter)
    raw, principal = {"synthetic": True}, object()
    if not enabled:
        expect_error("FILE_IMPORT_DISABLED", Runtime._invoke_async(runtime, name, raw, principal))
        assert calls == []
    else:
        result = asyncio.run(Runtime._invoke_async(runtime, name, raw, principal))
        assert result == {"route": "relay"}
        assert calls == [("relay", raw, principal)]


@pytest.mark.parametrize("redirect,allowed", [
    ("https://sdmntprnext.oaiusercontent.com/new-object", True),
    ("https://unapproved.invalid/private?sig=" + SOURCE_TICKET, False),
    ("https://sdmntprnext.blob.core.windows.net/private", False),
])
def test_real_downloader_revalidates_redirects_without_external_network(relay, monkeypatch, redirect, allowed):
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_PROVIDERS", '["openai_sediment"]')
    connections = []
    class Response:
        def __init__(self, initial):
            self.status = 302 if initial else 200
            self.done = False

        def getheader(self, name, default=None):
            return {"Location": redirect, "Content-Length": "3"}.get(name, default)

        def read1(self, size):
            if self.done:
                return b""
            self.done = True
            return b"abc"

    class Connection:
        def __init__(self, host, *args, **kwargs):
            self.host, self.sock, self.closed = host, None, False
            connections.append(self)

        def request(self, method, path, headers):
            assert method == "GET"
            assert set(headers) == {"Accept-Encoding", "User-Agent", "Connection"}

        def getresponse(self):
            return Response(len(connections) == 1)

        def close(self):
            self.closed = True

    monkeypatch.setattr(incoming_artifacts, "PublicTLSConnection", Connection)
    monkeypatch.setattr(native, "download_chunks", incoming_artifacts.download_chunks)
    if allowed:
        assert run_import(relay)["created"]
        assert len(connections) == 2
    else:
        error = expect_error("ARTIFACT_SOURCE_DENIED",
                             native.import_native_file(relay.runtime, request(), relay.principal))
        assert error.details["stage"] == "redirect_validation"
        assert len(connections) == 1 and not relay.runtime.calls
    assert all(connection.closed for connection in connections)


def save_finish_operation(relay, **changes):
    """Populate the durable table that the native recovery fast path reads."""
    name, args, identifier = next(call for call in reversed(relay.runtime.calls)
                                  if call[0] == "incoming_upload_finish")
    data = {**relay.runtime.operations[identifier][1], **changes}
    relay.runtime.persist_finish(identifier, args, data, relay.principal)
    return identifier


def test_completed_receipt_recovery_needs_no_live_node_or_source(relay):
    first = run_import(relay)
    save_finish_operation(relay)
    relay.runtime.connected = False
    value = request()
    value["file"]["download_url"] = "https://files.oaiusercontent.com/expired-object?sig=old"
    before = (len(relay.downloads), len(relay.runtime.calls), len(relay.temporary_files))
    recovered = run_import(relay, value)
    assert recovered["recovered"] is True
    assert recovered["upload_id"] == first["upload_id"]
    assert recovered["bytes"] == 3 and recovered["sha256"] == sha(b"abc")
    assert before == (len(relay.downloads), len(relay.runtime.calls), len(relay.temporary_files))


@pytest.mark.parametrize("change", [
    {"ready": False}, {"bytes": 2}, {"sha256": "0" * 64},
    {"path": "different.bin"}, {"received": 2}, {"upload_id": "f" * 32},
])
def test_completed_recovery_rejects_inconsistent_saved_receipt(relay, change):
    run_import(relay)
    save_finish_operation(relay, **change)
    before = len(relay.downloads)
    expect_error("INVALID_IMPORT_RECEIPT",
                 native.import_native_file(relay.runtime, request(), relay.principal))
    assert len(relay.downloads) == before


def test_completed_recovery_sanitizes_saved_node_fields(relay, caplog):
    run_import(relay)
    save_finish_operation(relay, download_url=SOURCE_URL, file_id=SOURCE_ID, ticket=SOURCE_TICKET,
                          output="untrusted node output", data="untrusted node payload")
    result = run_import(relay)
    assert result["created"]
    rendered = json.dumps(result) + caplog.text
    for private in (SOURCE_URL, SOURCE_ID, SOURCE_TICKET, "untrusted node output", "untrusted node payload"):
        assert private not in rendered


def test_completed_recovery_still_requires_current_permission(relay):
    run_import(relay)
    save_finish_operation(relay)
    relay.runtime.allowed = False
    before = (len(relay.downloads), len(relay.runtime.calls))
    expect_error("INVALID_TOKEN", native.import_native_file(relay.runtime, request(), relay.principal))
    assert before == (len(relay.downloads), len(relay.runtime.calls))


def test_file_identity_change_without_expected_hash_conflicts_before_fetch(relay):
    value = request()
    value.pop("expected_sha256")
    run_import(relay, value)
    value["file"]["file_id"] = "different-untrusted-source-id"
    before = len(relay.downloads)
    expect_error("IDEMPOTENCY_CONFLICT",
                 native.import_native_file(relay.runtime, value, relay.principal))
    assert len(relay.downloads) == before


def test_changed_url_same_file_identity_without_hash_can_resume(relay):
    value = request()
    value.pop("expected_sha256")
    first = run_import(relay, value)
    value["file"]["download_url"] = "https://cdn.openai.com/refreshed"
    assert run_import(relay, value)["upload_id"] == first["upload_id"]


def test_read_only_preflight_does_not_require_write_or_online_node(relay):
    principal = replace(relay.principal, scopes={"read"})
    relay.runtime.mapping["mode"] = "read"
    relay.runtime.connected = False
    result = asyncio.run(native.inspect_native_file(relay.runtime, inspect_request(), principal))
    assert result["checked"] and result["source_allowed"]
    assert not relay.downloads and not relay.runtime.calls


@pytest.mark.parametrize("tool", ["download_artifact", "inspect_file_source"])
@pytest.mark.parametrize("change", ["size", "file_id", "url", "extra"])
def test_invalid_native_metadata_reports_sanitized_validation_error(relay, tool, change, caplog):
    value = request() if tool == "download_artifact" else inspect_request()
    if change == "size":
        value["file"]["size"] = SOURCE_TICKET
    elif change == "file_id":
        value["file"]["file_id"] = {"private": SOURCE_ID}
    elif change == "url":
        value["file"]["download_url"] = {"private": SOURCE_URL}
    else:
        value["file"]["unexpected"] = SOURCE_TICKET
    function = native.import_native_file if tool == "download_artifact" else native.inspect_native_file
    error = expect_error("INVALID_ARGUMENTS", function(relay.runtime, value, relay.principal))
    rendered = str(error) + json.dumps(error.details) + caplog.text
    assert SOURCE_URL not in rendered and SOURCE_ID not in rendered and SOURCE_TICKET not in rendered
    assert not relay.downloads and not relay.runtime.calls


@pytest.mark.parametrize("name", ["download_artifact", "inspect_file_source"])
def test_runtime_never_falls_back_after_enabled_relay_denial(monkeypatch, name):
    monkeypatch.setenv("CODEPIER_FILE_IMPORT_STREAMING", "true")
    monkeypatch.setenv("CODEPIER_NATIVE_FILE_RELAY", "true")
    async def denied(*args):
        raise DevError("ARTIFACT_SOURCE_DENIED", "Synthetic policy rejection", 403)
    async def only_gate_read(function, *args):
        assert function is native.native_ingress_enabled
        return function(*args)
    def forbidden_legacy(*args):
        pytest.fail("A denied relay must not fall back to node-side fetching")
    runtime = SimpleNamespace(_loop=None,
        store=SimpleNamespace(run=only_gate_read, one=lambda *args: None), _invoke=forbidden_legacy)
    monkeypatch.setattr(native, "import_native_file", denied)
    monkeypatch.setattr(native, "inspect_native_file", denied)
    expect_error("ARTIFACT_SOURCE_DENIED", Runtime._invoke_async(runtime, name, {}, object()))


def test_actual_stream_limit_without_content_length_never_begins(relay, monkeypatch):
    class Response:
        status = 200
        def getheader(self, name, default=None):
            return default
        def read1(self, size):
            return b"abcd"

    connections = []
    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock, self.closed = None, False
            connections.append(self)
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            self.closed = True

    policy = {**native.native_source_policy(), "max_bytes": 3}
    monkeypatch.setattr(native, "native_source_policy", lambda store=None: policy)
    monkeypatch.setattr(incoming_artifacts, "PublicTLSConnection", Connection)
    monkeypatch.setattr(native, "download_chunks", incoming_artifacts.download_chunks)
    value = request()
    value["file"].pop("size")
    value.pop("expected_sha256")
    expect_error("ARTIFACT_TOO_LARGE", native.import_native_file(relay.runtime, value, relay.principal))
    assert connections and all(connection.closed for connection in connections)
    assert not relay.runtime.calls and not list(relay.root.iterdir())


@pytest.mark.parametrize("info", ["[]", "null", "1", '"synthetic-not-an-object"'])
def test_nonobject_node_metadata_fails_closed_before_source(relay, info):
    relay.store.execute("UPDATE devices SET info=?", (info,))
    expect_error("AGENT_UPGRADE_REQUIRED",
                 native.import_native_file(relay.runtime, request(), relay.principal))
    assert not relay.downloads and not relay.runtime.calls


def test_concurrent_identical_imports_share_mutation_keys_and_upload(relay):
    async def scenario():
        return await asyncio.gather(*[
            native.import_native_file(relay.runtime, request(), relay.principal) for _ in range(3)
        ])
    results = asyncio.run(scenario())
    assert len({result["upload_id"] for result in results}) == 1
    assert all(result["created"] for result in results)
    assert len(relay.store.all("SELECT * FROM incoming_file_imports")) == 1
    assert len(relay.store.all("SELECT * FROM native_file_ingress_requests")) == 1
    for tool in ("incoming_upload_begin", "incoming_upload_chunk", "incoming_upload_finish"):
        keys = {args["idempotency_key"] for name, args, _ in relay.runtime.calls if name == tool}
        operations = {identifier for name, _, identifier in relay.runtime.calls if name == tool}
        assert len(keys) == len(operations) == 1


def test_temporary_storage_failure_releases_capacity_and_stays_private(relay, monkeypatch, caplog):
    def temporary(*args, **kwargs):
        raise OSError(SOURCE_URL + " " + SOURCE_ID)
    monkeypatch.setattr(native.tempfile, "TemporaryFile", temporary)
    error = expect_error("FILE_IMPORT_STORAGE",
                         native.import_native_file(relay.runtime, request(), relay.principal))
    assert SOURCE_URL not in str(error) + caplog.text and SOURCE_ID not in str(error) + caplog.text
    assert not relay.downloads and not relay.runtime.calls
    assert relay.runtime._native_ingress_active == 0


def test_native_intent_record_quota_never_fetches(relay):
    asyncio.run(native.inspect_native_file(relay.runtime, inspect_request(), relay.principal))
    native._reserve_native(relay.runtime.incoming_files, request(), relay.principal)
    with relay.store.transaction(immediate=True):
        relay.store.execute(
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<4095) "
            "INSERT INTO native_file_ingress_requests(request_key,identity,created) SELECT printf('%064x',x),'synthetic',? FROM n",
            (time.time(),))
    value = request(idempotency_key="new-native-intent-key")
    expect_error("FILE_IMPORT_BUSY", native.import_native_file(relay.runtime, value, relay.principal))
    assert not relay.downloads and not relay.runtime.calls


def advance_completed_import_past_native_identity_ttl(relay, monkeypatch, *, with_hash=True, stale_manifest=False):
    """Begin on day zero, finish just before day one, collect on day 7.5."""
    day_zero = time.time()
    clock = [day_zero]
    monkeypatch.setattr(native.time, "time", lambda: clock[0])
    invoke = relay.runtime.invoke

    async def delayed_finish(name, args, principal):
        if name == "incoming_upload_finish":
            clock[0] = day_zero + 86400 - 1
        return await invoke(name, args, principal)

    monkeypatch.setattr(relay.runtime, "invoke", delayed_finish)
    value = request()
    if not with_hash:
        value.pop("expected_sha256")
    first = run_import(relay, value)
    save_finish_operation(relay)
    identity = relay.store.one("SELECT * FROM native_file_ingress_requests")
    manifest = relay.store.one("SELECT * FROM incoming_file_imports")
    assert identity["created"] == day_zero
    assert manifest["expires"] > day_zero + 7.5 * 86400
    if stale_manifest:
        # The Agent finished, but the Hub lost its final receipt reconciliation.
        relay.store.execute("UPDATE incoming_file_imports SET expires=?,state=\'reserved\'",
                            (day_zero + 86400,))
    # Include an unbound old record so this verifies collection actually ran.
    relay.store.execute("INSERT INTO native_file_ingress_requests(request_key,identity,created) VALUES(?,?,?)",
                        ("f" * 64, "synthetic-orphan", day_zero))
    clock[0] = day_zero + 7.5 * 86400
    native._reserve_native(relay.runtime.incoming_files,
                           request(idempotency_key="trigger-native-gc-key", path="new-reservation.bin"),
                           relay.principal)
    assert relay.store.one("SELECT * FROM native_file_ingress_requests WHERE request_key=?",
                           (identity["request_key"],)) == identity
    assert relay.store.one("SELECT * FROM native_file_ingress_requests WHERE request_key=?", ("f" * 64,)) is None
    return value, first


def test_native_identity_gc_preserves_still_recoverable_completed_receipt(relay, monkeypatch):
    value, first = advance_completed_import_past_native_identity_ttl(relay, monkeypatch)
    relay.runtime.connected = False
    before = (len(relay.downloads), len(relay.runtime.calls))
    recovered = run_import(relay, value)
    assert recovered["recovered"] and recovered["created"]
    assert recovered["upload_id"] == first["upload_id"]
    assert before == (len(relay.downloads), len(relay.runtime.calls))


@pytest.mark.parametrize("change", ["path", "expected-hash", "file-id"])
def test_native_identity_gc_cannot_enable_retarget_or_source_rebinding(relay, monkeypatch, change):
    value, _ = advance_completed_import_past_native_identity_ttl(
        relay, monkeypatch, with_hash=change != "file-id")
    if change == "path":
        value["path"] = "retargeted-after-gc.bin"
    elif change == "expected-hash":
        value["expected_sha256"] = "0" * 64
    else:
        value["file"]["file_id"] = "different-file-after-identity-ttl"
    before = (len(relay.downloads), len(relay.runtime.calls))
    expect_error("IDEMPOTENCY_CONFLICT",
                 native.import_native_file(relay.runtime, value, relay.principal))
    assert before == (len(relay.downloads), len(relay.runtime.calls))
    assert not (relay.root / "retargeted-after-gc.bin").exists()


@pytest.mark.parametrize("with_hash", [False, True])
def test_missing_native_identity_with_existing_manifest_fails_closed(relay, with_hash):
    value = request()
    if not with_hash:
        value.pop("expected_sha256")
    run_import(relay, value)
    save_finish_operation(relay)
    relay.store.execute("DELETE FROM native_file_ingress_requests")
    before = (len(relay.downloads), len(relay.runtime.calls))
    expect_error("IDEMPOTENCY_CONFLICT",
                 native.import_native_file(relay.runtime, value, relay.principal))
    assert not relay.store.all("SELECT * FROM native_file_ingress_requests")
    assert before == (len(relay.downloads), len(relay.runtime.calls))


@pytest.mark.parametrize("field,value", [
    ("path", "tampered-manifest-path.bin"), ("workspace_id", "a" * 32),
    ("bytes", 99), ("sha256", "0" * 64),
])
def test_recovery_independently_compares_native_request_with_current_manifest(relay, field, value):
    run_import(relay)
    save_finish_operation(relay)
    relay.store.execute("UPDATE incoming_file_imports SET " + field + "=?", (value,))
    before = (len(relay.downloads), len(relay.runtime.calls))
    expect_error("IDEMPOTENCY_CONFLICT",
                 native.import_native_file(relay.runtime, request(), relay.principal))
    assert before == (len(relay.downloads), len(relay.runtime.calls))


def test_native_identity_gc_preserves_stale_manifest_with_unreconciled_finish(relay, monkeypatch):
    value, first = advance_completed_import_past_native_identity_ttl(
        relay, monkeypatch, stale_manifest=True)
    manifest = relay.store.one("SELECT * FROM incoming_file_imports")
    assert manifest["expires"] < time.time()
    assert manifest["state"] == "reserved" and manifest["upload_id"] == first["upload_id"]
    assert relay.store.one("SELECT * FROM native_file_ingress_requests WHERE request_key=?",
                           (manifest["request_key"],)) is not None
    assert relay.store.one("SELECT * FROM operations WHERE tool='incoming_upload_finish'") is not None
    assert (relay.root / value["path"]).read_bytes() == b"abc"



def bypass_native_completed_fast_path(monkeypatch):
    """Exercise later adapter receipt paths while keeping real finish proof."""
    reserve = native._reserve_native

    def continue_from_begin(service, args, principal):
        current, project, _ = reserve(service, args, principal)
        return current, project, None

    monkeypatch.setattr(native, "_reserve_native", continue_from_begin)


def test_completed_begin_receipt_skips_redundant_status_and_finish(relay, monkeypatch):
    first = run_import(relay)
    bypass_native_completed_fast_path(monkeypatch)
    before = len(relay.runtime.calls)

    async def unexpected(*args, **kwargs):
        pytest.fail("A verified completed begin must not dispatch status or finish")

    monkeypatch.setattr(relay.runtime.incoming_files, "status", unexpected)
    monkeypatch.setattr(relay.runtime.incoming_files, "finish", unexpected)
    result = run_import(relay)
    assert result["created"] and result["upload_id"] == first["upload_id"]
    assert [(name, args["idempotency_key"]) for name, args, _ in relay.runtime.calls[before:]] == [
        ("incoming_upload_begin", request()["idempotency_key"]),
    ]


@pytest.mark.parametrize("receipt_change", [{}, {"ready": False}, {"received": 1}])
def test_completed_chunk_receipt_is_verified_and_stops_transfer(relay, monkeypatch, receipt_change):
    relay.payload = bytes(range(256)) * 2051
    first = run_import(relay)
    bypass_native_completed_fast_path(monkeypatch)
    service = relay.runtime.incoming_files
    stale = {**first, "created": False, "ready": False, "state": "receiving", "received": 0}
    chunks = []

    async def stale_begin(*args, **kwargs):
        return dict(stale)

    async def stale_status(*args, **kwargs):
        return dict(stale)

    async def completed_chunk(upload_id, offset, data, checksum, principal):
        chunks.append((upload_id, offset, len(data), checksum))
        return {**first, **receipt_change}

    async def unexpected_finish(*args, **kwargs):
        pytest.fail("A completed chunk receipt must not trigger another finish")

    monkeypatch.setattr(service, "begin", stale_begin)
    monkeypatch.setattr(service, "status", stale_status)
    monkeypatch.setattr(service, "chunk", completed_chunk)
    monkeypatch.setattr(service, "finish", unexpected_finish)
    if receipt_change:
        expect_error("INVALID_IMPORT_RECEIPT",
                     native.import_native_file(relay.runtime, request(relay.payload), relay.principal))
    else:
        result = run_import(relay)
        assert result["created"] and result["received"] == len(relay.payload)
        assert result["sha256"] == sha(relay.payload)
    assert len(chunks) == 1 and chunks[0][1:3] == (0, native.CHUNK)
    assert (relay.root / request()["path"]).read_bytes() == relay.payload


def test_native_lost_finish_reply_recovers_after_original_begin_expiry(relay, monkeypatch):
    value, first = advance_completed_import_past_native_identity_ttl(
        relay, monkeypatch, stale_manifest=True)
    old = relay.store.one("SELECT * FROM incoming_file_imports")
    assert old["expires"] < time.time() and old["state"] == "reserved"
    relay.runtime.connected = False
    before = (len(relay.downloads), len(relay.runtime.calls), len(relay.temporary_files))
    recovered = run_import(relay, value)
    assert recovered["recovered"] and recovered["created"] and recovered["ready"]
    assert recovered["upload_id"] == first["upload_id"]
    current = relay.store.one("SELECT * FROM incoming_file_imports")
    assert current["state"] == "finished"
    assert current["expires"] == first["expires"] > time.time()
    assert before == (len(relay.downloads), len(relay.runtime.calls), len(relay.temporary_files))


def reserve_intent(relay, key, *, principal=None, **changes):
    return native._reserve_native(relay.runtime.incoming_files,
                                  request(idempotency_key=key, **changes),
                                  principal or relay.principal)


def allow_quota_test_owner(relay, monkeypatch):
    """Keep project/scope checks while allowing a second synthetic identity."""
    authorize = relay.runtime.authorize

    def allowed(principal, scope, *, project_id=None):
        authorize(replace(principal, user_id="owner-a", space_id="legacy"),
                  scope, project_id=project_id)

    monkeypatch.setattr(relay.runtime, "authorize", allowed)


@pytest.mark.parametrize("reason,code", [
    ("denied-url", "ARTIFACT_SOURCE_DENIED"),
    ("oversize", "ARTIFACT_TOO_LARGE"),
    ("offline", "FILE_IMPORT_DEVICE_OFFLINE"),
    ("unsupported", "AGENT_UPGRADE_REQUIRED"),
])
def test_new_rejected_intents_never_consume_identity_rows(relay, reason, code):
    value = request()
    if reason == "denied-url":
        value["file"]["download_url"] = "https://unapproved.invalid/file"
    elif reason == "oversize":
        value["file"]["size"] = native.native_source_policy()["max_bytes"] + 1
    elif reason == "offline":
        relay.runtime.connected = False
    else:
        relay.store.execute("UPDATE devices SET info=?", ('{"capabilities":[]}',))
    for index in range(5):
        value["idempotency_key"] = "rejected-native-intent-" + str(index)
        expect_error(code, native.import_native_file(relay.runtime, value, relay.principal))
    assert not relay.store.all("SELECT * FROM native_file_ingress_requests")
    assert not relay.store.all("SELECT * FROM incoming_file_imports")
    assert not relay.downloads and not relay.runtime.calls


def test_native_new_key_hourly_limit_precedes_fetch_and_persists(relay, monkeypatch):
    monkeypatch.setattr(native, "NATIVE_OWNER_HOURLY", 2)
    reserve_intent(relay, "native-hourly-one")
    reserve_intent(relay, "native-hourly-two")
    relay.runtime.incoming_files = IncomingFileService(relay.runtime)
    expect_error("FILE_IMPORT_BUSY", native.import_native_file(
        relay.runtime, request(idempotency_key="native-hourly-three"), relay.principal))
    assert not relay.downloads and not relay.runtime.calls
    before = relay.store.all("SELECT * FROM native_file_ingress_requests ORDER BY request_key")
    reserve_intent(relay, "native-hourly-one")
    assert relay.store.all("SELECT * FROM native_file_ingress_requests ORDER BY request_key") == before
    assert all(row["owner_user_id"] == "owner-a" and row["space_id"] == "legacy" for row in before)


def test_native_hourly_window_reopens_without_dropping_identity(relay, monkeypatch):
    monkeypatch.setattr(native, "NATIVE_OWNER_HOURLY", 1)
    reserve_intent(relay, "native-old-hour-window")
    relay.store.execute("UPDATE native_file_ingress_requests SET created=created-3601")
    original = relay.store.one("SELECT * FROM native_file_ingress_requests")
    reserve_intent(relay, "native-next-hour-window")
    assert relay.store.one("SELECT * FROM native_file_ingress_requests WHERE request_key=?",
                           (original["request_key"],)) == original
    assert len(relay.store.all("SELECT * FROM native_file_ingress_requests")) == 2


def test_native_pending_limit_counts_only_unadmitted_intents(relay, monkeypatch):
    monkeypatch.setattr(native, "NATIVE_OWNER_PENDING", 1)
    completed = run_import(relay)
    assert completed["created"]
    reserve_intent(relay, "native-unadmitted-one", path="pending-one.bin")
    before = len(relay.downloads)
    expect_error("FILE_IMPORT_BUSY", native.import_native_file(
        relay.runtime, request(idempotency_key="native-unadmitted-two", path="pending-two.bin"),
        relay.principal))
    assert len(relay.downloads) == before
    assert len(relay.store.all("SELECT * FROM native_file_ingress_requests")) == 2


def test_native_total_limit_includes_completed_identity_rows(relay, monkeypatch):
    monkeypatch.setattr(native, "NATIVE_OWNER_RECORDS", 2)
    for index in range(2):
        assert run_import(relay, request(idempotency_key="native-complete-" + str(index),
                                         path="completed-" + str(index) + ".bin"))["created"]
    before = (len(relay.downloads), len(relay.runtime.calls))
    expect_error("FILE_IMPORT_BUSY", native.import_native_file(
        relay.runtime, request(idempotency_key="native-complete-overflow", path="overflow.bin"),
        relay.principal))
    assert before == (len(relay.downloads), len(relay.runtime.calls))
    assert len(relay.store.all("SELECT * FROM native_file_ingress_requests")) == 2


def test_native_owner_quota_isolated_and_not_reset_by_another_actor(relay, monkeypatch):
    allow_quota_test_owner(relay, monkeypatch)
    monkeypatch.setattr(native, "NATIVE_OWNER_RECORDS", 1)
    reserve_intent(relay, "native-owner-a-first")
    with pytest.raises(DevError, match="owner quota"):
        reserve_intent(relay, "native-owner-a-other-actor",
                       principal=replace(relay.principal, actor="second-credential-same-owner"))
    other = replace(relay.principal, actor="other-owner", user_id="owner-b")
    reserve_intent(relay, "native-owner-b-first", principal=other)
    rows = relay.store.all("SELECT * FROM native_file_ingress_requests")
    assert {row["owner_user_id"] for row in rows} == {"owner-a", "owner-b"}
    assert len(rows) == 2


def test_native_owner_quotas_are_scoped_to_space_and_user(relay, monkeypatch):
    allow_quota_test_owner(relay, monkeypatch)
    monkeypatch.setattr(native, "NATIVE_OWNER_PENDING", 1)
    reserve_intent(relay, "native-first-space-key")
    other = replace(relay.principal, space_id="second-space")
    reserve_intent(relay, "native-second-space-key", principal=other)
    assert {row["space_id"] for row in relay.store.all("SELECT * FROM native_file_ingress_requests")} == {
        "legacy", "second-space"}


def test_completed_recovery_bypasses_full_new_intent_quotas_and_stale_url(relay, monkeypatch):
    value = request()
    first = run_import(relay, value)
    save_finish_operation(relay)
    before = relay.store.one("SELECT * FROM native_file_ingress_requests")
    for name in ("NATIVE_OWNER_HOURLY", "NATIVE_OWNER_PENDING", "NATIVE_OWNER_RECORDS", "NATIVE_GLOBAL_RECORDS"):
        monkeypatch.setattr(native, name, 0)
    relay.runtime.connected = False
    value["file"]["download_url"] = "https://expired-unapproved.invalid/old"
    calls = (len(relay.downloads), len(relay.runtime.calls))
    recovered = run_import(relay, value)
    assert recovered["recovered"] and recovered["upload_id"] == first["upload_id"]
    assert calls == (len(relay.downloads), len(relay.runtime.calls))
    assert relay.store.one("SELECT * FROM native_file_ingress_requests") == before


def make_legacy_native_table(relay, rows):
    relay.store.execute("DROP TABLE IF EXISTS native_file_ingress_requests")
    relay.store.execute("""CREATE TABLE native_file_ingress_requests (
        request_key TEXT PRIMARY KEY,identity TEXT NOT NULL,created REAL NOT NULL)""")
    for row in rows:
        relay.store.execute("INSERT INTO native_file_ingress_requests VALUES(?,?,?)",
                            (row["request_key"], row["identity"], row["created"]))


def test_legacy_owner_migration_uses_manifest_and_preserves_unattributed_rows(relay):
    run_import(relay)
    known = relay.store.one("SELECT * FROM native_file_ingress_requests")
    unknown = {"request_key": "f" * 64, "identity": "legacy-unattributed", "created": time.time()}
    make_legacy_native_table(relay, [known, unknown])
    native._native_schema(relay.runtime.incoming_files)
    native._native_schema(relay.runtime.incoming_files)
    rows = {row["request_key"]: row for row in relay.store.all("SELECT * FROM native_file_ingress_requests")}
    assert rows[known["request_key"]] == known
    assert rows[unknown["request_key"]] == {**unknown, "owner_user_id": None, "space_id": None}


def test_legacy_unattributed_exact_recovery_can_bind_owner_without_new_quota(relay, monkeypatch):
    reserve_intent(relay, "native-legacy-original")
    original = relay.store.one("SELECT * FROM native_file_ingress_requests")
    make_legacy_native_table(relay, [original])
    for name in ("NATIVE_OWNER_HOURLY", "NATIVE_OWNER_PENDING", "NATIVE_OWNER_RECORDS", "NATIVE_GLOBAL_RECORDS"):
        monkeypatch.setattr(native, name, 0)
    reserve_intent(relay, "native-legacy-original")
    assert relay.store.one("SELECT * FROM native_file_ingress_requests") == original


def test_legacy_mismatched_identity_cannot_be_claimed_or_rewritten(relay):
    reserve_intent(relay, "native-legacy-conflict")
    original = relay.store.one("SELECT * FROM native_file_ingress_requests")
    make_legacy_native_table(relay, [original])
    with pytest.raises(DevError) as error:
        reserve_intent(relay, "native-legacy-conflict", path="changed.bin")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    row = relay.store.one("SELECT * FROM native_file_ingress_requests")
    assert row == {**original, "owner_user_id": None, "space_id": None}


@pytest.mark.parametrize("column,value", [("owner_user_id", "another-owner"), ("space_id", "another-space")])
def test_native_persisted_owner_binding_cannot_be_reassigned(relay, column, value):
    reserve_intent(relay, "native-owner-binding")
    relay.store.execute("UPDATE native_file_ingress_requests SET " + column + "=?", (value,))
    before = relay.store.one("SELECT * FROM native_file_ingress_requests")
    with pytest.raises(DevError) as error:
        reserve_intent(relay, "native-owner-binding")
    assert error.value.code == "FILE_IMPORT_NOT_FOUND"
    assert relay.store.one("SELECT * FROM native_file_ingress_requests") == before


def test_legacy_unattributed_rows_still_count_toward_global_cap(relay, monkeypatch):
    legacy = {"request_key": "e" * 64, "identity": "legacy-unattributed", "created": time.time()}
    make_legacy_native_table(relay, [legacy])
    monkeypatch.setattr(native, "NATIVE_GLOBAL_RECORDS", 1)
    expect_error("FILE_IMPORT_BUSY", native.import_native_file(
        relay.runtime, request(idempotency_key="native-global-overflow"), relay.principal))
    row = relay.store.one("SELECT * FROM native_file_ingress_requests")
    assert row == {**legacy, "owner_user_id": None, "space_id": None}
    assert not relay.downloads and not relay.runtime.calls


def test_native_quota_reservation_is_atomic_for_concurrent_new_keys(relay, monkeypatch):
    monkeypatch.setattr(native, "NATIVE_OWNER_PENDING", 1)

    async def scenario():
        return await asyncio.gather(*[
            relay.store.run(native._reserve_native, relay.runtime.incoming_files,
                            request(idempotency_key="native-racing-key-" + str(index)),
                            relay.principal)
            for index in range(4)], return_exceptions=True)

    results = asyncio.run(scenario())
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert all(not isinstance(result, Exception) or isinstance(result, DevError) and result.code == "FILE_IMPORT_BUSY"
               for result in results)
    assert len(relay.store.all("SELECT * FROM native_file_ingress_requests")) == 1
    assert not relay.downloads and not relay.runtime.calls
