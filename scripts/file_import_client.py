"""Authenticated local byte ingress, without source paths/content in MCP messages.

Read roots are explicit owner configuration, never tool arguments. The OS read
boundary still applies. Unsupported safe-handle platforms fail closed.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from shared.mcp_protocol import MODERN, PREFIX, request_headers

MAX_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
MAX_ATTEMPTS = 6
PENDING_STATES = {"queued", "running", "reconnecting", "cancelling", "unknown"}
FAILED_STATES = {"failed", "cancelled", "needs_review", "interrupted"}
_IDENTIFIER = re.compile(r"[a-f0-9]{32}")


class FileImportError(ValueError):
    """Safe metadata for a JSON-RPC error; never include exception/body text."""
    def __init__(self, code, message, *, retryable=False, operation_id="", upload_id=""):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.operation_id = operation_id
        self.upload_id = upload_id


def _fail(code, message, **kwargs):
    raise FileImportError(code, message, **kwargs) from None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_identity(info):
    return info.st_dev, info.st_ino, info.st_mode


# Keep this dependency-free projection in parity with agent.filesystem.protected.
# Importing FileEngine here would add cryptography to the httpx/pydantic-only bridge.
_PROTECTED_DIRECTORIES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".ssh", ".aws", ".azure",
    ".kube", ".codepier-agent", ".remote-dev-agent", ".remote-dev", ".codepier", ".cache",
}
_PROTECTED_NAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519", "auth.json",
    "credentials.json", ".credentials.json", "secrets.json",
}


def _protected_local_path(path):
    parts = tuple(part.lower() for part in PurePosixPath(path).parts)
    private_runtime = {".codex": {"sessions", "archived_sessions"}, ".claude": {"projects"}}
    if any(parts[index + 1] in private_runtime.get(part, ())
           for index, part in enumerate(parts[:-1])):
        return True
    return any(
        part in _PROTECTED_DIRECTORIES or part.startswith(".rd-") or part in _PROTECTED_NAMES
        or part.startswith(".env.") and not part.endswith((".example", ".sample", ".template"))
        or part.endswith((".pem", ".key", ".p12", ".pfx"))
        for part in parts
    )


def _check_source_protection(source, roots, info=None):
    # Inspect the whole absolute ancestry, not only a root-relative filename:
    # configuring ~/.ssh itself as an allowed root must not remove protection.
    if _protected_local_path(source.as_posix()) or any(_protected_local_path(root.as_posix()) for root in roots):
        _fail("PROTECTED_LOCAL_SOURCE", "Credential and internal paths cannot be imported")
    for name in ("CODEPIER_TOKEN_FILE", "REMOTE_DEV_TOKEN_FILE"):
        configured = os.environ.get(name)
        if not configured:
            continue
        try:
            token_path = Path(configured).expanduser().resolve(strict=False)
            if source == token_path:
                _fail("PROTECTED_LOCAL_SOURCE", "The bridge credential cannot be imported")
            # A case alias or alternative link cannot disguise the configured PAT.
            if info is not None:
                try:
                    token_info = token_path.stat()
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino) == (token_info.st_dev, token_info.st_ino):
                    _fail("PROTECTED_LOCAL_SOURCE", "The bridge credential cannot be imported")
        except FileImportError:
            raise
        except (OSError, RuntimeError, ValueError):
            _fail("LOCAL_READ_DENIED", "The configured credential boundary cannot be verified")


@contextmanager
def _source_descriptor(source, allowed_roots):
    """Pin every no-follow ancestor and file descriptor before reading bytes."""
    source = Path(source)
    roots = [Path(root) for root in allowed_roots]
    if (not roots or not source.is_absolute() or ".." in source.parts
            or any(not root.is_absolute() or ".." in root.parts for root in roots)
            or not any(source != root and source.is_relative_to(root) for root in roots)):
        _fail("LOCAL_READ_DENIED", "Source is outside explicitly configured local read roots")
    _check_source_protection(source, roots)
    if (os.name != "posix" or not getattr(os, "O_NOFOLLOW", 0)
            or not getattr(os, "O_DIRECTORY", 0) or os.open not in os.supports_dir_fd):
        _fail("UNSUPPORTED_LOCAL_PLATFORM", "Secure local import requires no-follow directory handles")
    descriptors, links = [], []
    try:
        directory = os.open(source.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for name in source.parts[1:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(child)
            links.append((directory, name, _directory_identity(os.fstat(child))))
            directory = child
        fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(fd)
        baseline = os.fstat(fd)
        if not stat.S_ISREG(baseline.st_mode) or baseline.st_nlink != 1:
            _fail("UNSAFE_LOCAL_FILE", "Source must be a regular file with exactly one link")
        if not 0 <= baseline.st_size <= MAX_BYTES:
            _fail("LOCAL_FILE_TOO_LARGE", "Source exceeds the 512 MiB local import limit")

        def unchanged():
            current = os.fstat(fd)
            _check_source_protection(source, roots, current)
            entry = os.stat(source.name, dir_fd=directory, follow_symlinks=False)
            if _identity(current) != _identity(baseline) or _identity(entry) != _identity(baseline):
                _fail("LOCAL_FILE_CHANGED", "Source changed during import; publication will not be requested")
            for parent, name, identity in links:
                now = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _directory_identity(now) != identity or not stat.S_ISDIR(now.st_mode):
                    _fail("LOCAL_FILE_CHANGED", "Source ancestry changed during import")

        unchanged()
        yield fd, baseline.st_size, unchanged
    except OSError:
        _fail("LOCAL_READ_DENIED", "Source cannot be safely read within the configured local roots")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _hash_source(fd, size, unchanged):
    unchanged()
    os.lseek(fd, 0, os.SEEK_SET)
    digest, read = hashlib.sha256(), 0
    while read < size:
        unchanged()
        chunk = os.read(fd, min(CHUNK_BYTES, size - read))
        if not chunk:
            _fail("LOCAL_FILE_CHANGED", "Source length changed during import")
        digest.update(chunk)
        read += len(chunk)
        unchanged()
    if os.read(fd, 1):
        _fail("LOCAL_FILE_CHANGED", "Source length changed during import")
    unchanged()
    return digest.hexdigest()


def _reject_constant(_value):
    _fail("INVALID_RESPONSE", "Hub returned invalid JSON")


def _base_url(base):
    try:
        parsed = urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError
        _ = parsed.port
    except (TypeError, ValueError):
        _fail("INVALID_HUB_URL", "Hub URL must be HTTP(S) without credentials or query")
    return base.rstrip("/")


class _Session:
    def __init__(self, client, base, headers, retry_seconds, project, workspace_id):
        self.client, self.base = client, _base_url(base)
        self.headers = httpx.Headers(headers)
        self.headers["Cookie"] = ""
        self.headers["Accept"] = "application/json"
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            _fail("AUTHORIZATION_REQUIRED", "Local import requires the existing Bearer credential")
        self.retry_seconds = retry_seconds
        self.project, self.workspace_id = project, workspace_id
        self.upload_id = ""

    def request(self, method, path, *, body=None, content=None, chunk_hash=None, deadline=None, headers=None):
        deadline = deadline if deadline is not None else time.monotonic() + self.retry_seconds
        outgoing = self.headers.copy()
        if headers:
            outgoing.update(headers)
        outgoing["Content-Type"] = "application/octet-stream" if content is not None else "application/json"
        if chunk_hash is not None:
            outgoing["X-Chunk-Sha256"] = chunk_hash
        for attempt in range(MAX_ATTEMPTS):
            left = deadline - time.monotonic()
            if left <= 0:
                _fail("IMPORT_TIMEOUT", "Import deadline reached; resume only with the same key",
                      retryable=True, upload_id=self.upload_id)
            response = None
            try:
                response = self.client.request(method, self.base + path, headers=outgoing,
                    json=body, content=content, follow_redirects=False, auth=None,
                    timeout=httpx.Timeout(min(12, left), connect=min(4, left)))
            except httpx.TransportError:
                pass
            else:
                status = response.status_code
                if 300 <= status < 400:
                    _fail("REDIRECT_DENIED", "Hub import redirects are not allowed", upload_id=self.upload_id)
                retryable = status in {408, 429} or 500 <= status <= 599
                if retryable:
                    try:
                        error = response.json().get("error", {})
                        code = error.get("code", "") if isinstance(error, dict) else ""
                    except (ValueError, AttributeError, RecursionError):
                        code = ""
                    # A logical refusal is never made retryable by a 5xx wrapper.
                    if isinstance(code, str) and any(part in code for part in
                            ("DENIED", "FORBIDDEN", "CONFLICT", "MISMATCH", "INSUFFICIENT_SCOPE", "READ_ONLY")):
                        _fail("IMPORT_REJECTED", "Hub rejected the file import", upload_id=self.upload_id)
                if 200 <= status < 300:
                    try:
                        payload = response.json(parse_constant=_reject_constant)
                    except (ValueError, RecursionError):
                        _fail("INVALID_RESPONSE", "Hub returned invalid import JSON", upload_id=self.upload_id)
                    if not isinstance(payload, dict):
                        _fail("INVALID_RESPONSE", "Hub import response must be an object", upload_id=self.upload_id)
                    return payload
                if not retryable:
                    _fail("HTTP_" + str(status), "Hub rejected file import (HTTP " + str(status) + ")",
                          upload_id=self.upload_id)
            remaining = deadline - time.monotonic()
            if attempt + 1 >= MAX_ATTEMPTS or remaining <= 0:
                _fail("IMPORT_TRANSPORT_FAILED", "Import response unavailable; resume only with the same key",
                      retryable=True, upload_id=self.upload_id)
            delay = min(4, .25 * (2 ** attempt))
            if response is not None:
                try:
                    after = float(response.headers.get("Retry-After", "0"))
                    if math.isfinite(after):
                        delay = max(delay, min(5, after))
                except ValueError:
                    pass
            time.sleep(min(delay, remaining))
        raise AssertionError("unreachable")

    def settle(self, receipt):
        """Only consume known receipt schemas; never follow returned URLs/tools."""
        if not isinstance(receipt, dict) or receipt.get("error"):
            _fail("IMPORT_REJECTED", "Hub rejected the import operation", upload_id=self.upload_id)
        operation_id = receipt.get("operation_id", receipt.get("id"))
        if not isinstance(operation_id, str) or not _IDENTIFIER.fullmatch(operation_id):
            _fail("INVALID_RESPONSE", "Import receipt has no valid operation ID", upload_id=self.upload_id)
        deadline, round_number = time.monotonic() + self.retry_seconds, 0
        while True:
            if receipt.get("operation_id", receipt.get("id")) != operation_id:
                _fail("INVALID_RESPONSE", "Hub returned a different operation", upload_id=self.upload_id)
            state = receipt.get("state")
            if state in FAILED_STATES or receipt.get("error"):
                _fail("IMPORT_REJECTED", "File import operation failed",
                      operation_id=operation_id, upload_id=self.upload_id)
            # HTTP endpoints return Runtime.unwrap's flat successful metadata.
            if state in {"receiving", "ready", "complete"} and receipt.get("pending") is not True:
                return receipt, operation_id
            if state == "succeeded" and receipt.get("pending") is not True:
                result = receipt.get("result")
                if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("data"), dict):
                    _fail("INVALID_RESPONSE", "Import operation has no verified result",
                          operation_id=operation_id, upload_id=self.upload_id)
                return result["data"], operation_id
            if state not in PENDING_STATES:
                _fail("INVALID_RESPONSE", "Unknown import operation state",
                      operation_id=operation_id, upload_id=self.upload_id)
            if time.monotonic() >= deadline:
                _fail("IMPORT_PENDING", "Import is still pending; recover this exact operation",
                      retryable=True, operation_id=operation_id, upload_id=self.upload_id)
            request_id = "file-import-" + operation_id + "-" + str(round_number)
            args = {"operation": "wait", "operation_ids": [operation_id], "wait_seconds": 10,
                    "include_result": True, "include_output": False, "project": self.project}
            if self.workspace_id:
                args["workspace_id"] = self.workspace_id
            request = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                       "params": {"name": "process", "arguments": args}}
            extra = {"Accept": "application/json, text/event-stream"}
            if self.headers.get("MCP-Protocol-Version") == MODERN:
                request["params"]["_meta"] = {PREFIX + "protocolVersion": MODERN, PREFIX + "clientCapabilities": {}}
                extra.update(request_headers(request))
            try:
                payload = self.request("POST", "/mcp", body=request, headers=extra, deadline=deadline)
            except FileImportError as exc:
                exc.operation_id = operation_id
                raise
            if (payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id
                    or "error" in payload or not isinstance(payload.get("result"), dict)):
                _fail("INVALID_RESPONSE", "Invalid MCP process response",
                      operation_id=operation_id, upload_id=self.upload_id)
            result = payload["result"]
            structured = result.get("structuredContent")
            if result.get("isError") or not isinstance(structured, dict):
                _fail("IMPORT_REJECTED", "MCP process wait was rejected",
                      operation_id=operation_id, upload_id=self.upload_id)
            if isinstance(structured.get("content"), dict):
                structured = structured["content"]
            operations = structured.get("operations")
            if not isinstance(operations, list) or len(operations) != 1 or not isinstance(operations[0], dict):
                _fail("INVALID_RESPONSE", "MCP wait did not return the exact operation",
                      operation_id=operation_id, upload_id=self.upload_id)
            receipt = operations[0]
            round_number += 1
            if receipt.get("state") in PENDING_STATES:
                time.sleep(min(.1, max(0, deadline - time.monotonic())))


def _verified_data(data, *, upload_id, destination, size, digest, final=False):
    if (data.get("upload_id") != upload_id or data.get("path") != destination
            or type(data.get("bytes")) is not int or data["bytes"] != size
            or data.get("sha256") != digest or type(data.get("received")) is not int
            or not 0 <= data["received"] <= size):
        _fail("RECEIPT_MISMATCH", "Import receipt does not match the requested file", upload_id=upload_id)
    expires = data.get("expires")
    if (type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time()
            or type(data.get("created")) is not bool or type(data.get("ready")) is not bool):
        _fail("IMPORT_NOT_READY", "Upload has expired or has invalid receipt metadata", upload_id=upload_id)
    if data.get("state") not in {"receiving", "ready", "complete"}:
        _fail("IMPORT_NOT_READY", "Upload is expired, ambiguous, or not usable", upload_id=upload_id)
    if final or data.get("created") is True or data.get("state") == "complete":
        if (data.get("created") is not True or data.get("ready") is not True
                or data.get("state") != "complete" or data["received"] != size):
            _fail("IMPORT_NOT_COMMITTED", "File has no verified saved receipt", upload_id=upload_id)
    return data


def _public_receipt(data, operation_id):
    fields = ("upload_id", "path", "bytes", "received", "sha256", "state", "expires", "created", "ready")
    return {**{key: data[key] for key in fields}, "operation_id": operation_id}


def upload_local_file(client, base, headers, *, source: Path, project: str,
                      destination: str, idempotency_key: str, workspace_id: str = "",
                      allowed_roots: list[Path] = (), retry_seconds=30):
    """Return verified saved metadata, never merely queued/accepted/upload ID.

    retry_seconds bounds each durable step and its transport retries. Reuse the
    same immutable arguments/key to resume. Roots MUST be owner configuration.
    """
    if (not isinstance(project, str) or not project or not isinstance(idempotency_key, str)
            or not idempotency_key or not isinstance(workspace_id, str)):
        _fail("INVALID_ARGUMENTS", "Project and stable idempotency key are required")
    if (not isinstance(destination, str) or not destination or "\\" in destination
            or ":" in destination or "\0" in destination or PurePosixPath(destination).is_absolute()
            or any(part in {"", ".", ".."} for part in destination.split("/"))):
        _fail("INVALID_DESTINATION", "Destination must be a canonical project-relative file path")
    if (isinstance(retry_seconds, bool) or not isinstance(retry_seconds, (int, float))
            or not math.isfinite(retry_seconds) or not 0 < retry_seconds <= 120):
        _fail("INVALID_ARGUMENTS", "Retry duration must be finite and within 120 seconds")
    session = _Session(client, base, headers, retry_seconds, project, workspace_id)
    with _source_descriptor(source, allowed_roots) as (fd, size, unchanged):
        digest = _hash_source(fd, size, unchanged)
        begin = session.request("POST", "/api/file-imports", body={
            "project": project, "path": destination, "size": size, "sha256": digest,
            "idempotency_key": idempotency_key, "workspace_id": workspace_id})
        upload_id = begin.get("upload_id")
        if (not isinstance(upload_id, str) or not _IDENTIFIER.fullmatch(upload_id)
                or begin.get("operation_id") != upload_id):
            _fail("INVALID_RESPONSE", "Hub returned an invalid upload ID")
        session.upload_id = upload_id
        data, operation_id = session.settle(begin)
        verify = dict(upload_id=upload_id, destination=destination, size=size, digest=digest)
        _verified_data(data, **verify)
        unchanged()
        # A retried begin may carry the canonical committed receipt. Do not
        # start another status operation once saved metadata is verified.
        if data.get("created") is True:
            return _public_receipt(data, operation_id)
        data, operation_id = session.settle(session.request("GET", "/api/file-imports/" + upload_id))
        _verified_data(data, **verify)
        unchanged()
        if data.get("created") is True:
            return _public_receipt(data, operation_id)
        offset = data["received"]
        os.lseek(fd, offset, os.SEEK_SET)
        while offset < size:
            unchanged()
            chunk = os.read(fd, min(CHUNK_BYTES, size - offset))
            if not chunk:
                _fail("LOCAL_FILE_CHANGED", "Source length changed during import", upload_id=upload_id)
            unchanged()
            data, operation_id = session.settle(session.request(
                "PUT", "/api/file-imports/" + upload_id + "/chunks?offset=" + str(offset),
                content=chunk, chunk_hash=hashlib.sha256(chunk).hexdigest()))
            _verified_data(data, **verify)
            unchanged()
            # A concurrent same-key caller can finish while this chunk reply
            # is in flight. A verified monotonic receipt needs no further writes.
            if data.get("created") is True:
                return _public_receipt(data, operation_id)
            if data["received"] != offset + len(chunk):
                _fail("INVALID_PROGRESS", "Hub acknowledged unexpected file progress", upload_id=upload_id)
            offset = data["received"]
            unchanged()
        if _hash_source(fd, size, unchanged) != digest:
            _fail("LOCAL_FILE_CHANGED", "Source content changed during import", upload_id=upload_id)
        unchanged()
        data, operation_id = session.settle(session.request("POST", "/api/file-imports/" + upload_id + "/finish"))
        _verified_data(data, final=True, **verify)
        return _public_receipt(data, operation_id)
