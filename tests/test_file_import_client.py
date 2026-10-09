"""Local adapter contracts; no live network, credentials, or production files."""
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from scripts import file_import_client as adapter

UPLOAD = "a" * 32
TOKEN = "Bearer rd_test_secret_do_not_log"
DESTINATION = "资料/任意文件.unknown"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX safe-handle adapter")


class Clock:
    def __init__(self):
        self.now = 0.

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    # Unit cases must not inspect this executor's real configured credential.
    monkeypatch.delenv("CODEPIER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("REMOTE_DEV_TOKEN_FILE", raising=False)
    value = Clock()
    monkeypatch.setattr(adapter.time, "monotonic", value.monotonic)
    monkeypatch.setattr(adapter.time, "sleep", value.sleep)
    return value


class Hub:
    def __init__(self, payload, *, received=0, pending=False, wrap=False):
        self.payload = payload
        self.received = received
        self.pending = pending
        self.wrap = wrap
        self.requests = []
        self.chunks = []
        self.finished = False
        self.operations = {}
        self.begin_body = None
        self.wait_calls = []
        self.failures = {}
        self.modifier = None
        self.always_pending = False

    def data(self):
        return {"upload_id": UPLOAD, "path": DESTINATION, "bytes": len(self.payload),
                "received": self.received, "sha256": hashlib.sha256(self.payload).hexdigest(),
                "state": "complete" if self.finished else "receiving",
                "created": self.finished, "ready": self.finished, "expires": 9999999999}

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        key = (request.method, path)
        if path == "/mcp":
            accepted = {part.strip() for part in request.headers["Accept"].split(",")}
            # hub/mcp.py rejects either legacy or modern waits without both.
            assert {"application/json", "text/event-stream"} <= accepted
            wire = json.loads(request.content)
            self.wait_calls.append(wire)
            args = wire["params"]["arguments"]
            assert wire["method"] == "tools/call" and wire["params"]["name"] == "process"
            assert args["operation"] == "wait" and args["wait_seconds"] == 10
            assert args["include_output"] is False
            identifier = args["operation_ids"][0]
            assert len(args["operation_ids"]) == 1 and identifier in self.operations
            operation = {"operation_id": identifier, "state": "succeeded", "pending": False,
                         "result": {"ok": True, "data": self.operations[identifier]}}
            if self.always_pending:
                operation = {"operation_id": identifier, "state": "running", "pending": True}
            structured = {"operations": [operation]}
            if self.wrap:
                structured = {"content": structured}
            response = {"jsonrpc": "2.0", "id": wire["id"],
                        "result": {"structuredContent": structured, "isError": False}}
        else:
            if path == "/api/file-imports":
                body = json.loads(request.content)
                assert body == {"project": "project", "workspace_id": "",
                                "path": DESTINATION, "size": len(self.payload),
                                "sha256": hashlib.sha256(self.payload).hexdigest(),
                                "idempotency_key": "stable-upload-key"}
                if self.begin_body is not None:
                    assert body == self.begin_body
                self.begin_body = body
                identifier = UPLOAD
            elif request.method == "GET":
                assert path == "/api/file-imports/" + UPLOAD
                identifier = "b" * 32
            elif path.endswith("/chunks"):
                offset = int(request.url.params["offset"])
                chunk = request.content
                assert 0 < len(chunk) <= adapter.CHUNK_BYTES
                assert request.headers["X-Chunk-Sha256"] == hashlib.sha256(chunk).hexdigest()
                assert request.headers["Content-Type"] == "application/octet-stream"
                assert self.payload[offset:offset + len(chunk)] == chunk
                assert offset <= self.received
                self.received = max(self.received, offset + len(chunk))
                self.chunks.append((offset, chunk))
                identifier = hashlib.md5(str(offset).encode(), usedforsecurity=False).hexdigest()
            else:
                assert path == "/api/file-imports/" + UPLOAD + "/finish"
                assert not request.content
                assert self.received == len(self.payload)
                self.finished = True
                identifier = "f" * 32
            data = self.data()
            self.operations[identifier] = data
            response = {"operation_id": identifier, **data}
            if self.pending:
                response = {"operation_id": identifier, "upload_id": UPLOAD,
                            "state": "queued", "pending": True}
        if self.modifier:
            response = self.modifier(request, response)
        if self.failures.get(key, 0):
            self.failures[key] -= 1
            raise httpx.ReadError("deliberately lost response containing " + TOKEN, request=request)
        return httpx.Response(200, content=json.dumps(response).encode(), headers={"Content-Type": "application/json"})


def run(tmp_path, hub, *, source=None, roots=None, headers=None, **kwargs):
    source = source or tmp_path.resolve() / "输入 文档.binary"
    if not source.exists():
        source.write_bytes(hub.payload)
    with httpx.Client(transport=httpx.MockTransport(hub), follow_redirects=True,
                      cookies={"unwanted": "cookie"}) as client:
        return adapter.upload_local_file(client, "https://hub.example",
            headers or {"Authorization": TOKEN, "MCP-Protocol-Version": "2025-11-25"},
            source=source, project="project", destination=DESTINATION,
            idempotency_key="stable-upload-key",
            allowed_roots=[tmp_path.resolve()] if roots is None else roots, **kwargs)


@pytest.mark.parametrize("payload", [b"", b"\x00\xff\xfe\x80binary", "文字🙂".encode(),
                                      bytes(range(256)) * 2200])
def test_round_trip_bytes_and_metadata(tmp_path, payload, capsys):
    hub = Hub(payload)
    result = run(tmp_path, hub)
    assert result["created"] is True and result["ready"] is True
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["bytes"] == len(payload)
    assert b"".join(chunk for _, chunk in hub.chunks) == payload
    assert max((len(chunk) for _, chunk in hub.chunks), default=0) <= adapter.CHUNK_BYTES
    assert len(hub.chunks) == (len(payload) + adapter.CHUNK_BYTES - 1) // adapter.CHUNK_BYTES
    for request in hub.requests:
        assert request.headers["Authorization"] == TOKEN
        assert not request.headers.get("Cookie")
        assert "token" not in str(request.url) and "secret" not in str(request.url)
        assert str(tmp_path.resolve()).encode() not in request.content
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("wrapped", [False, True])
def test_pending_exact_operations(tmp_path, wrapped):
    hub = Hub(b"pending-data", pending=True, wrap=wrapped)
    result = run(tmp_path, hub)
    assert result["created"]
    assert len(hub.wait_calls) == 4
    for wire in hub.wait_calls:
        assert wire["params"]["arguments"]["operation_ids"][0] in hub.operations


def test_pending_modern_mcp_metadata(tmp_path):
    hub = Hub(b"modern", pending=True)
    run(tmp_path, hub, headers={"Authorization": TOKEN, "MCP-Protocol-Version": adapter.MODERN})
    waits = [request for request in hub.requests if request.url.path == "/mcp"]
    assert waits and all(r.headers["Mcp-Name"] == "process" for r in waits)
    assert all(r.headers["Mcp-Method"] == "tools/call" for r in waits)
    assert hub.wait_calls[0]["params"]["_meta"][adapter.PREFIX + "protocolVersion"] == adapter.MODERN


def test_resume_durable_offset_without_repeating_saved_bytes(tmp_path):
    payload = bytes(range(256)) * 2100
    hub = Hub(payload, received=300000)
    run(tmp_path, hub)
    assert hub.chunks[0][0] == 300000
    assert b"".join(chunk for _, chunk in hub.chunks) == payload[300000:]


def test_repeated_transport_reuses_exact_begin_and_chunk(tmp_path):
    hub = Hub(b"same bytes")
    hub.failures[("POST", "/api/file-imports")] = 2
    hub.failures[("PUT", "/api/file-imports/" + UPLOAD + "/chunks")] = 2
    result = run(tmp_path, hub)
    assert result["created"]
    begins = [r for r in hub.requests if r.url.path == "/api/file-imports"]
    chunks = [r for r in hub.requests if r.method == "PUT"]
    assert len(begins) == len(chunks) == 3
    assert len({r.content for r in begins}) == len({r.content for r in chunks}) == 1
    assert len({str(r.url) for r in chunks}) == 1


def test_lost_wait_reply_reuses_json_rpc_id(tmp_path):
    hub = Hub(b"x", pending=True)
    hub.failures[("POST", "/mcp")] = 2
    run(tmp_path, hub)
    assert hub.wait_calls[0] == hub.wait_calls[1] == hub.wait_calls[2]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 413, 422])
def test_rejections_are_not_retried(tmp_path, status):
    calls = []
    def reject(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "secret/file/content"})
    hub = Hub(b"x")
    hub.__class__ = type("RejectedHub", (Hub,), {"__call__": lambda self, req: reject(req)})
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub)
    assert caught.value.code == "HTTP_" + str(status)
    assert not caught.value.retryable and len(calls) == 1
    assert "secret/file/content" not in str(caught.value)


@pytest.mark.parametrize("status", [408, 429, 500, 501, 502, 503, 504, 599])
def test_transient_statuses_are_bounded(tmp_path, status, clock):
    hub = Hub(b"x")
    requests = []
    def reject(self, req):
        requests.append(req)
        return httpx.Response(status, headers={"Retry-After": "0"})
    hub.__class__ = type("TransientHub", (Hub,), {"__call__": reject})
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub)
    assert caught.value.retryable and len(requests) == adapter.MAX_ATTEMPTS
    assert clock.now <= 30


def test_pending_is_not_saved_success(tmp_path, clock):
    hub = Hub(b"x", pending=True)
    hub.always_pending = True
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub, retry_seconds=.31)
    assert caught.value.code == "IMPORT_PENDING"
    assert caught.value.operation_id == UPLOAD
    assert not hub.finished and not hub.chunks and clock.now <= .31


def test_redirects_are_never_followed(tmp_path):
    hub = Hub(b"x")
    calls = []
    def redirect(self, request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.invalid/upload"})
    hub.__class__ = type("RedirectHub", (Hub,), {"__call__": redirect})
    with pytest.raises(adapter.FileImportError, match="redirects"):
        run(tmp_path, hub)
    assert len(calls) == 1 and calls[0].url.host == "hub.example"


@pytest.mark.parametrize("changes", [
    {"sha256": "0" * 64}, {"bytes": 999}, {"received": -1}, {"received": 2},
    {"received": True}, {"path": "other"}, {"upload_id": "e" * 32},
])
def test_status_metadata_must_match(tmp_path, changes):
    hub = Hub(b"x")
    hub.modifier = lambda req, data: {**data, **changes} if req.method == "GET" else data
    with pytest.raises(adapter.FileImportError):
        run(tmp_path, hub)
    assert not hub.finished and not hub.chunks


@pytest.mark.parametrize("changes", [{"created": False}, {"created": 1}, {"ready": False},
                                      {"state": "receiving"}, {"state": "ambiguous"},
                                      {"state": "expired"}, {"sha256": "0" * 64}])
def test_finish_requires_verified_commit(tmp_path, changes):
    hub = Hub(b"x")
    hub.modifier = lambda req, data: {**data, **changes} if req.url.path.endswith("/finish") else data
    with pytest.raises(adapter.FileImportError):
        run(tmp_path, hub)


def test_root_allowlist_default_denies_before_network(tmp_path):
    hub = Hub(b"private")
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub, roots=[])
    assert caught.value.code == "LOCAL_READ_DENIED" and not hub.requests


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "hardlink", "fifo", "directory", "outside", "relative"])
def test_unsafe_sources_are_never_sent(tmp_path, kind):
    root = tmp_path.resolve()
    source = root / "source"
    source.write_bytes(b"x")
    roots = [root]
    if kind == "symlink":
        link = root / "link"
        link.symlink_to(source)
        source = link
    elif kind == "parent_symlink":
        real = root / "actual"
        real.mkdir()
        source.rename(real / "source")
        link = root / "alias"
        link.symlink_to(real, target_is_directory=True)
        source = link / "source"
    elif kind == "hardlink":
        os.link(source, root / "hard")
    elif kind == "fifo":
        source.unlink()
        os.mkfifo(source)
    elif kind == "directory":
        source.unlink()
        source.mkdir()
    elif kind == "outside":
        child = root / "allowed"
        child.mkdir()
        roots = [child]
    elif kind == "relative":
        source = Path("relative-not-authorized")
    hub = Hub(b"x")
    with httpx.Client(transport=httpx.MockTransport(hub)) as client:
        with pytest.raises(adapter.FileImportError):
            adapter.upload_local_file(client, "https://hub.example", {"Authorization": TOKEN},
                source=source, project="project", destination=DESTINATION,
                idempotency_key="stable-upload-key", allowed_roots=roots)
    assert not hub.requests


@pytest.mark.parametrize("when", ["begin", "chunk"])
@pytest.mark.parametrize("change", ["write", "replace", "ancestor"])
def test_source_mutation_aborts_before_finish(tmp_path, when, change):
    root = tmp_path.resolve()
    folder = root / "folder"
    folder.mkdir()
    source = folder / "source"
    payload = b"x" * (adapter.CHUNK_BYTES + 3)
    source.write_bytes(payload)
    hub = Hub(payload)
    changed = False
    def modify(request, response):
        nonlocal changed
        trigger = request.url.path == "/api/file-imports" if when == "begin" else request.method == "PUT"
        if trigger and not changed:
            changed = True
            if change == "write":
                source.write_bytes(b"y" * len(payload))
            elif change == "replace":
                replacement = folder / "other"
                replacement.write_bytes(payload)
                replacement.replace(source)
            else:
                folder.rename(root / "moved")
                folder.mkdir()
                (folder / "source").write_bytes(payload)
        return response
    hub.modifier = modify
    with pytest.raises(adapter.FileImportError):
        run(tmp_path, hub, source=source)
    assert not hub.finished


def test_oversize_rejected_before_read_or_network(tmp_path):
    source = tmp_path.resolve() / "large"
    with source.open("wb") as stream:
        stream.truncate(adapter.MAX_BYTES + 1)
    hub = Hub(b"")
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub, source=source)
    assert caught.value.code == "LOCAL_FILE_TOO_LARGE" and not hub.requests


def test_changed_file_during_hash_is_not_sent(tmp_path, monkeypatch):
    source = tmp_path.resolve() / "source"
    source.write_bytes(b"first")
    original = adapter.os.read
    changed = False
    def read(fd, count):
        nonlocal changed
        result = original(fd, count)
        if not changed:
            changed = True
            source.write_bytes(b"later")
        return result
    monkeypatch.setattr(adapter.os, "read", read)
    hub = Hub(b"first")
    with pytest.raises(adapter.FileImportError):
        run(tmp_path, hub, source=source)
    assert not hub.requests


def test_permission_denial_is_preserved_and_sanitized(tmp_path, monkeypatch):
    def denied(fd, count):
        raise PermissionError("secret local path")
    monkeypatch.setattr(adapter.os, "read", denied)
    hub = Hub(b"x")
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub)
    assert caught.value.code == "LOCAL_READ_DENIED"
    assert "secret local path" not in str(caught.value) and not hub.requests


@pytest.mark.parametrize("bad_base", ["https://hub.example?token=secret",
                                     "https://user:secret@hub.example", "file:///tmp/source"])
def test_credentials_in_url_denied(tmp_path, bad_base):
    source = tmp_path.resolve() / "source"
    source.write_bytes(b"x")
    hub = Hub(b"x")
    with httpx.Client(transport=httpx.MockTransport(hub)) as client:
        with pytest.raises(adapter.FileImportError):
            adapter.upload_local_file(client, bad_base, {"Authorization": TOKEN},
                source=source, project="project", destination=DESTINATION,
                idempotency_key="stable-upload-key", allowed_roots=[tmp_path.resolve()])
    assert not hub.requests


def test_only_allowlisted_metadata_is_returned(tmp_path):
    hub = Hub(b"secret content")
    hub.modifier = lambda req, data: {**data, "content": "do not disclose",
        "uploadUrl": "https://attacker.invalid/?token=secret", "data_base64": "nope"}
    result = run(tmp_path, hub)
    assert set(result) == {"upload_id", "path", "bytes", "received", "sha256",
                           "state", "expires", "created", "ready", "operation_id"}
    assert all(request.url.host == "hub.example" for request in hub.requests)


@pytest.mark.parametrize("changes", [{"expires": 0}, {"expires": float("inf")},
                                      {"expires": True}, {"created": 0}, {"ready": None}])
def test_expired_or_invalid_receipts_are_not_usable(tmp_path, changes):
    hub = Hub(b"x")
    hub.modifier = lambda req, data: {**data, **changes} if req.method == "GET" else data
    with pytest.raises(adapter.FileImportError):
        run(tmp_path, hub)
    assert not hub.finished and not hub.chunks


def test_logical_conflicts_wrapped_in_503_are_not_retried(tmp_path):
    hub = Hub(b"x")
    calls = []
    def rejected(self, request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"code": "FILE_IMPORT_HASH_MISMATCH",
                                                  "message": TOKEN}})
    hub.__class__ = type("RefusedHub", (Hub,), {"__call__": rejected})
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub)
    assert not caught.value.retryable and len(calls) == 1 and TOKEN not in str(caught.value)


def test_process_transport_failure_retains_exact_operation(tmp_path):
    hub = Hub(b"x", pending=True)
    hub.failures[("POST", "/mcp")] = 99
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub)
    assert caught.value.operation_id == caught.value.upload_id == UPLOAD
    assert caught.value.retryable


def test_already_committed_retry_returns_receipt_without_finishing_again(tmp_path):
    hub = Hub(b"x", received=1)
    hub.finished = True
    result = run(tmp_path, hub)
    assert result["created"] is True and not hub.chunks
    assert len(hub.requests) == 1 and hub.requests[0].url.path == "/api/file-imports"
    assert result["operation_id"] == UPLOAD


def test_wrong_operation_from_process_is_rejected(tmp_path):
    hub = Hub(b"x", pending=True)
    def wrong(request, result):
        if request.url.path == "/mcp":
            result["result"]["structuredContent"]["operations"][0]["operation_id"] = "e" * 32
        return result
    hub.modifier = wrong
    with pytest.raises(adapter.FileImportError, match="different operation"):
        run(tmp_path, hub)
    assert not hub.finished and not hub.chunks


PROTECTED_SAMPLES = [
    ".env", ".env.production", ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519",
    "auth.json", "credentials.json", ".credentials.json", "secrets.json",
    "client.pem", "private.key", "certificate.p12", "certificate.pfx",
    ".ssh/config", ".aws/config", ".azure/profile", ".kube/config",
    ".codex/sessions/fake-session", ".codex/archived_sessions/fake-session",
    ".claude/projects/fake-session", ".codepier-agent/config.json",
    ".remote-dev-agent/state.db", ".remote-dev/state", ".codepier/state",
    ".rd-backup/private", ".git/config",
]


@pytest.mark.parametrize("relative", PROTECTED_SAMPLES)
def test_protected_local_sources_never_read_or_send(tmp_path, monkeypatch, relative):
    source = tmp_path.resolve() / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"synthetic placeholder only")
    hub = Hub(b"synthetic placeholder only")
    def forbid_read(*args):
        pytest.fail("Protected source bytes were read")
    monkeypatch.setattr(adapter.os, "read", forbid_read)
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub, source=source)
    assert caught.value.code == "PROTECTED_LOCAL_SOURCE"
    assert not hub.requests


@pytest.mark.parametrize("relative", [".ssh", ".codex/sessions", ".codepier-agent", ".remote-dev"])
def test_protected_allowed_root_cannot_remove_ancestor_boundary(tmp_path, monkeypatch, relative):
    root = tmp_path.resolve() / relative
    root.mkdir(parents=True)
    source = root / "innocent.bin"
    source.write_bytes(b"synthetic placeholder only")
    hub = Hub(b"synthetic placeholder only")
    def forbid_read(*args):
        pytest.fail("Protected-root bytes were read")
    monkeypatch.setattr(adapter.os, "read", forbid_read)
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub, source=source, roots=[root])
    assert caught.value.code == "PROTECTED_LOCAL_SOURCE" and not hub.requests


@pytest.mark.parametrize("environment_name", ["CODEPIER_TOKEN_FILE", "REMOTE_DEV_TOKEN_FILE"])
@pytest.mark.parametrize("alias", [False, True])
def test_custom_named_bridge_token_cannot_be_uploaded(tmp_path, monkeypatch, environment_name, alias):
    source = tmp_path.resolve() / "innocent.txt"
    source.write_bytes(b"synthetic placeholder only")
    configured = source
    if alias:
        configured = tmp_path.resolve() / "configured-alias"
        configured.symlink_to(source)
    monkeypatch.setenv(environment_name, str(configured))
    hub = Hub(b"synthetic placeholder only")
    def forbid_read(*args):
        pytest.fail("Configured credential bytes were read")
    monkeypatch.setattr(adapter.os, "read", forbid_read)
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub, source=source)
    assert caught.value.code == "PROTECTED_LOCAL_SOURCE" and not hub.requests


def test_local_protection_matches_existing_filesystem_policy():
    from agent.filesystem import IGNORED_DIRS, protected
    examples = PROTECTED_SAMPLES + [name + "/example.txt" for name in IGNORED_DIRS]
    examples += ["generated/report.pdf", ".env.example", ".env.dev.sample",
                 ".env.dev.template", ".codex/skills/guide.md", "empty.data", "图像.png"]
    for value in examples:
        for path in (value, "/synthetic/home/project/" + value, value.upper()):
            assert adapter._protected_local_path(path) == protected(path)


@pytest.mark.parametrize("name", [".env.example", ".env.dev.sample", "ordinary.pdf"])
def test_safe_generated_files_remain_importable(tmp_path, name):
    source = tmp_path.resolve() / name
    source.write_bytes(b"ordinary generated content")
    assert run(tmp_path, Hub(b"ordinary generated content"), source=source)["created"]


@pytest.mark.parametrize("changes", [
    {"created": False}, {"created": 1}, {"ready": False}, {"state": "receiving"},
    {"sha256": "0" * 64}, {"bytes": 99}, {"received": 0}, {"expires": 0},
])
def test_invalid_complete_begin_never_gets_or_writes(tmp_path, changes):
    hub = Hub(b"x", received=1)
    hub.finished = True
    hub.modifier = lambda request, data: {**data, **changes}
    with pytest.raises(adapter.FileImportError):
        run(tmp_path, hub)
    assert len(hub.requests) == 1 and hub.requests[0].url.path == "/api/file-imports"
    assert not hub.chunks


@pytest.mark.parametrize("completion", ["e" * 32, "https://attacker.invalid/?token=secret",
                                        {"content": "never disclose"}])
def test_complete_begin_keeps_only_allowlisted_receipt(tmp_path, completion):
    hub = Hub(b"x", received=1)
    hub.finished = True
    hub.modifier = lambda request, data: {**data, "completion_operation_id": completion,
                                         "content": "never disclose"}
    result = run(tmp_path, hub)
    assert result["operation_id"] == UPLOAD
    assert "completion_operation_id" not in result and "content" not in result
    assert len(hub.requests) == 1


def test_complete_begin_still_checks_local_source_identity(tmp_path):
    source = tmp_path.resolve() / "source.bin"
    source.write_bytes(b"x")
    hub = Hub(b"x", received=1)
    hub.finished = True
    def changed(request, data):
        source.write_bytes(b"changed")
        return data
    hub.modifier = changed
    with pytest.raises(adapter.FileImportError) as caught:
        run(tmp_path, hub, source=source)
    assert caught.value.code == "LOCAL_FILE_CHANGED"
    assert len(hub.requests) == 1 and not hub.chunks


def test_concurrent_complete_chunk_returns_without_more_writes(tmp_path):
    hub = Hub(b"x" * (adapter.CHUNK_BYTES + 10))
    def completed(request, data):
        if request.method == "PUT":
            return {**data, "received": len(hub.payload), "state": "complete",
                    "created": True, "ready": True, "completion_operation_id": "f" * 32}
        return data
    hub.modifier = completed
    result = run(tmp_path, hub)
    assert result["created"] is True and result["received"] == len(hub.payload)
    assert "completion_operation_id" not in result
    assert len(hub.chunks) == 1 and not hub.finished
    assert [request.method for request in hub.requests] == ["POST", "GET", "PUT"]


@pytest.mark.parametrize("changes", [{"created": False}, {"ready": False}, {"received": 1},
                                      {"sha256": "0" * 64}, {"path": "other.bin"}])
def test_invalid_complete_chunk_is_rejected(tmp_path, changes):
    hub = Hub(b"x" * (adapter.CHUNK_BYTES + 10))
    def completed(request, data):
        if request.method == "PUT":
            return {**data, "received": len(hub.payload), "state": "complete",
                    "created": True, "ready": True, **changes}
        return data
    hub.modifier = completed
    with pytest.raises(adapter.FileImportError):
        run(tmp_path, hub)
    assert len(hub.chunks) == 1 and not hub.finished
