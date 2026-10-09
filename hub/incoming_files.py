"""Authenticated, bounded byte ingress. This is not a native-file URL relay.

The existing HTTP credential authorizes every request. IDs are private record
identifiers, never upload capabilities. Only encrypted Runtime payloads carry
the internal base64 chunk transport; public receipts and this table are metadata.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hub.db_worker import run_db
from hub.principal import refresh_principal
from shared.util import DevError

CHUNK = 256 * 1024
MAX_BYTES = 512 * 1024 * 1024
MAX_JSON = 64 * 1024
TTL = 24 * 3600
RETENTION = 7 * 24 * 3600
CLEANUP_BATCH = 100
MAX_ACTIVE = 8
MAX_GLOBAL_ACTIVE = 256
MAX_BEGIN_HOURLY = 60
MAX_RECORDS = 100000
MAX_INFLIGHT = 8
BODY_TIMEOUT = 30
TOOLS = frozenset("incoming_upload_" + name for name in ("begin", "status", "chunk", "finish"))
_HEX = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[a-f0-9]{32}")
# A terminal RPC failure does not by itself prove the upload is terminal.
# These begin denials occur before spool/publication in the current protocol.
_BEGIN_DENIALS = frozenset({
    "INVALID_UPLOAD", "INVALID_PATH", "PROTECTED_PATH", "INVALID_ROOT",
    "ROOT_MISSING", "PROTECTED_ROOT", "ROOT_NOT_ALLOWED", "READ_ONLY",
    "SYMLINK_BLOCKED", "OUTSIDE_PROJECT", "UPLOAD_CONFIGURATION",
    "UPLOAD_TOO_LARGE", "UPLOAD_FORBIDDEN", "UPLOAD_QUOTA",
    "ARTIFACT_DESTINATION_EXISTS", "INTEGRATION_DISABLED", "UPLOAD_DISABLED", "FILE_IMPORT_DISABLED",
    "WORKSPACE_NOT_FOUND", "WORKSPACE_NOT_READY", "WORKSPACE_CHANGED",
})
_RECOVERABLE = frozenset({
    "UPLOAD_OFFSET", "UPLOAD_CONFLICT", "UPLOAD_CHUNK_INTEGRITY",
    "INVALID_UPLOAD_CHUNK", "UPLOAD_INCOMPLETE", "UPLOAD_COMPLETE",
    "UPLOAD_JOURNAL_BUSY", "ARTIFACT_DESTINATION_EXISTS", "UPLOAD_FORBIDDEN",
    "READ_ONLY", "ROOT_NOT_ALLOWED", "WORKSPACE_NOT_READY",
})
_FIELDS = ("actor", "grant_id", "owner_user_id", "space_id", "project_id",
           "device_id", "root", "workspace_id", "path", "bytes", "sha256", "begin_key")


def ingress_enabled(store=None):
    """Resolve the same persistent policy used by the instance settings panel."""
    from hub.file_import_settings import enabled
    return enabled("streaming_enabled", store)


def _error(code, message, status=400):
    raise DevError(code, message, status)


def _text(value, name, low=1, high=128):
    if (not isinstance(value, str) or not low <= len(value) <= high
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _error("INVALID_IMPORT", "Invalid " + name)
    return value


def begin_arguments(value):
    required = {"project", "idempotency_key", "path", "size", "sha256"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {"workspace_id"}:
        _error("INVALID_IMPORT", "Expected project, idempotency_key, path, size, sha256 and optional workspace_id")
    value = dict(value)
    _text(value["project"], "project", high=200)
    _text(value["idempotency_key"], "idempotency_key", 8)
    path = _text(value["path"], "path", high=1024)
    if (path.startswith(("/", "\\")) or "\\" in path or ":" in path
            or PureWindowsPath(path).drive or any(p in {"", ".", ".."} for p in path.split("/"))
            or PurePosixPath(path).is_absolute()):
        _error("INVALID_IMPORT_PATH", "Destination must be an unambiguous project-relative file path")
    if type(value["size"]) is not int or not 0 <= value["size"] <= MAX_BYTES:
        _error("INVALID_IMPORT_SIZE", "File size must be between zero and 512 MiB")
    if not isinstance(value["sha256"], str) or not _HEX.fullmatch(value["sha256"]):
        _error("INVALID_IMPORT_HASH", "Expected lowercase SHA-256")
    workspace = value.setdefault("workspace_id", "")
    if not isinstance(workspace, str) or workspace and not _ID.fullmatch(workspace):
        _error("INVALID_IMPORT", "Invalid workspace_id")
    return value


def _request_key(principal, key):
    return hashlib.sha256(json.dumps([principal.space_id, principal.actor, key],
                                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _step_key(upload_id, action, *parts):
    value = json.dumps([upload_id, action, *parts], separators=(",", ":"))
    return "file-import-" + hashlib.sha256(value.encode()).hexdigest()


class IncomingFileService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.inflight = 0
        self.remote_inflight = 0
        self.store.execute("""CREATE TABLE IF NOT EXISTS incoming_file_imports (
            request_key TEXT PRIMARY KEY, upload_id TEXT UNIQUE,
            actor TEXT NOT NULL, grant_id TEXT, owner_user_id TEXT NOT NULL,
            space_id TEXT NOT NULL, project_id TEXT NOT NULL, device_id TEXT NOT NULL,
            root TEXT NOT NULL, workspace_id TEXT NOT NULL, path TEXT NOT NULL,
            bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, begin_key TEXT NOT NULL,
            created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'reserved')""")
        self.store.execute("""CREATE INDEX IF NOT EXISTS incoming_file_imports_scope
            ON incoming_file_imports(space_id,owner_user_id,actor,created)""")
        self.store.execute("""CREATE INDEX IF NOT EXISTS incoming_file_imports_expiry
            ON incoming_file_imports(expires)""")

    def principal(self, auth, request):
        if not ingress_enabled(self.store):
            _error("FILE_IMPORT_DISABLED", "Streaming file import is not enabled", 404)
        principal = (auth.bearer(request) if request.headers.get("authorization")
                     else auth.panel(request, write=True))
        principal = refresh_principal(self.store, principal)
        if "write" not in principal.scopes:
            _error("INSUFFICIENT_SCOPE", "File import requires write permission", 403)
        return principal

    def project(self, value, principal):
        if not ingress_enabled(self.store):
            _error("FILE_IMPORT_DISABLED", "Streaming file import is not enabled", 404)
        principal = refresh_principal(self.store, principal)
        if "write" not in principal.scopes:
            _error("INSUFFICIENT_SCOPE", "File import requires write permission", 403)
        project = self.runtime.project(value, principal)
        self.runtime.authorize(principal, "write", project_id=project["id"])
        if project.get("mode") != "write" or not project.get("device_enabled", True):
            _error("PROJECT_READ_ONLY", "Project is not currently writable", 403)
        return principal, project

    @staticmethod
    def _owner(row, principal):
        return (row["actor"] == principal.actor and row["grant_id"] == principal.grant_id
                and row["owner_user_id"] == principal.user_id and row["space_id"] == principal.space_id)

    def _check(self, row, principal, *, project=None):
        if not row or not self._owner(row, principal):
            _error("FILE_IMPORT_NOT_FOUND", "File import was not found for this credential", 404)
        principal, current = self.project(row["project_id"], principal)
        if not self._owner(row, principal):
            _error("FILE_IMPORT_NOT_FOUND", "File import was not found for this credential", 404)
        for candidate in (current, project):
            if candidate is not None and any(candidate[k] != row[field]
                    for k, field in (("id", "project_id"), ("root", "root"), ("device_id", "device_id"))):
                _error("FILE_IMPORT_MAPPING_CHANGED", "Original project mapping changed", 409)
        # A lost finish response must not shorten the Agent's durable receipt
        # to the original 24-hour transfer lease. Authority and mapping were
        # checked above, before consulting any saved completion evidence.
        row = self._recover_completion(row)
        if row["expires"] <= time.time() and row["state"] != "recovery_required":
            _error("FILE_IMPORT_EXPIRED", "File import expired", 410)
        return row, principal, current

    def checked(self, auth, request, upload_id):
        if not _ID.fullmatch(upload_id):
            _error("FILE_IMPORT_NOT_FOUND", "File import was not found", 404)
        principal = self.principal(auth, request)
        row = self.store.one("SELECT * FROM incoming_file_imports WHERE upload_id=?", (upload_id,))
        row, principal, project = self._check(row, principal)
        # Optional selectors may assert the original context, never switch it.
        if (request.query_params.get("project") not in (None, row["project_id"])
                or request.query_params.get("workspace_id") not in (None, row["workspace_id"])):
            _error("FILE_IMPORT_CONTEXT_CHANGED", "Use the original project and workspace", 409)
        return row, principal, project

    def reserve(self, auth, request, args):
        return self.reserve_for(self.principal(auth, request), args)

    def reserve_for(self, principal, args):
        principal, project = self.project(args["project"], principal)
        key = _request_key(principal, args["idempotency_key"])
        desired = dict(actor=principal.actor, grant_id=principal.grant_id,
                       owner_user_id=principal.user_id, space_id=principal.space_id,
                       project_id=project["id"], device_id=project["device_id"], root=project["root"],
                       workspace_id=args["workspace_id"], path=args["path"], bytes=args["size"],
                       sha256=args["sha256"], begin_key=args["idempotency_key"])
        with self.store.transaction(immediate=True):
            old = self.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (key,))
            if old:
                self._check(old, principal, project=project)
                if any(old[k] != desired[k] for k in _FIELDS):
                    _error("IDEMPOTENCY_CONFLICT", "Import key already identifies different immutable metadata", 409)
                return old, principal
            self.cleanup()
            self._refresh_owner(principal)
            now = time.time()
            counts = self.store.one("""SELECT
                SUM(CASE WHEN state='recovery_required' OR (expires>? AND state NOT IN ('finished','failed')) THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN created>? THEN 1 ELSE 0 END) AS recent
                FROM incoming_file_imports WHERE space_id=? AND owner_user_id=?""",
                (now, now - 3600, principal.space_id, principal.user_id))
            global_count = self.store.one("""SELECT COUNT(*) AS n FROM incoming_file_imports
                WHERE state='recovery_required' OR (expires>? AND state NOT IN ('finished','failed'))""", (now,))["n"]
            total = self.store.one("SELECT COUNT(*) AS n FROM incoming_file_imports")["n"]
            if (total >= MAX_RECORDS or (counts["active"] or 0) >= MAX_ACTIVE or (counts["recent"] or 0) >= MAX_BEGIN_HOURLY
                    or global_count >= MAX_GLOBAL_ACTIVE):
                _error("FILE_IMPORT_BUSY", "File import quota reached; resume existing imports", 429)
            self.store.execute("INSERT INTO incoming_file_imports(request_key," + ",".join(_FIELDS)
                + ",created,expires) VALUES(" + ",".join("?" for _ in range(len(_FIELDS) + 3)) + ")",
                (key, *(desired[k] for k in _FIELDS), now, now + TTL))
            return self.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (key,)), principal

    def validate_admission(self, name, args, project, principal):
        """Runtime calls this inside operation admission's Store lock.

        This closes the HTTP validation-to-dispatch mapping race and prevents
        calling internal RPCs as an alternative, unbound upload interface.
        """
        if name not in TOOLS:
            return
        if not ingress_enabled(self.store):
            _error("FILE_IMPORT_DISABLED", "Streaming file import is not enabled", 404)
        principal = refresh_principal(self.store, principal)
        if name == "incoming_upload_begin":
            row = self.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?",
                                  (_request_key(principal, args.get("idempotency_key")),))
            self._check(row, principal, project=project)
            if any(args.get(k, "") != row[field] for k, field in (
                    ("path", "path"), ("size", "bytes"), ("sha256", "sha256"),
                    ("workspace_id", "workspace_id"), ("idempotency_key", "begin_key"))):
                _error("IDEMPOTENCY_CONFLICT", "Import metadata does not match the reservation", 409)
        else:
            row = self.store.one("SELECT * FROM incoming_file_imports WHERE upload_id=?", (args.get("upload_id"),))
            self._check(row, principal, project=project)
            if args.get("workspace_id", "") != row["workspace_id"]:
                _error("FILE_IMPORT_CONTEXT_CHANGED", "Original workspace required", 409)
        if args.get("project") != row["project_id"]:
            _error("FILE_IMPORT_CONTEXT_CHANGED", "Original project required", 409)

    def authorize_operation(self, operation, principal):
        """A durable receipt still requires its original upload binding."""
        current = self.store.one(
            "SELECT * FROM operations WHERE id=?", (operation['id'],))
        if current['tool'] == 'incoming_upload_begin':
            row = self.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?",
                                  (_request_key(principal, current['idem']),))
        else:
            try:
                upload_id = json.loads(current['args_summary'])['upload_id']
            except (ValueError, TypeError, KeyError):
                _error("FILE_IMPORT_NOT_FOUND", "Upload receipt binding is unavailable", 404)
            row = self.store.one("SELECT * FROM incoming_file_imports WHERE upload_id=?", (upload_id,))
        self._check(row, principal)
        self._record_operation(row, current)


    @staticmethod
    def _matches_operation(row, operation):
        if not operation or any(operation.get(k) != row[k] for k in
                ("actor", "grant_id", "owner_user_id", "space_id", "project_id", "device_id")):
            return False
        try:
            summary = json.loads(operation["args_summary"])
        except (ValueError, TypeError):
            return False
        if not isinstance(summary, dict) or summary.get("workspace_id", "") != row["workspace_id"]:
            return False
        if operation["tool"] == "incoming_upload_begin":
            return (operation["idem"] == row["begin_key"] and all(
                summary.get(key) == row[field] for key, field in
                (("path", "path"), ("size", "bytes"), ("sha256", "sha256"))))
        return operation["tool"] in TOOLS and summary.get("upload_id") == row["upload_id"]


    def _completion_receipt(self, row, operation=None):
        """Read only the exact validated canonical finish; never invent success."""
        if not row["upload_id"]:
            return None
        if operation is None:
            operation = self.store.one("""SELECT * FROM operations
                WHERE space_id=? AND actor=? AND idem=? AND tool='incoming_upload_finish'""",
                (row["space_id"], row["actor"], _step_key(row["upload_id"], "finish")))
        if (not self._matches_operation(row, operation)
                or operation["tool"] != "incoming_upload_finish" or operation["state"] != "succeeded"):
            return None
        try:
            result = json.loads(operation["result"])
            data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if (data.get("state") != "complete" or data.get("created") is not True or data.get("ready") is not True
                or data.get("upload_id") != row["upload_id"]
                or any(data.get(k) != row[k] for k in ("path", "bytes", "sha256"))
                or type(data.get("bytes")) is not int or type(data.get("received")) is not int
                or data["received"] != row["bytes"]):
            return None
        expires, updated = data.get("expires"), operation.get("updated")
        if (type(expires) not in (int, float) or not math.isfinite(expires)
                or type(updated) not in (int, float) or not math.isfinite(updated)
                or not time.time() < expires <= updated + RETENTION + 1):
            return None
        fields = ("upload_id", "path", "bytes", "received", "sha256",
                  "state", "ready", "created", "expires")
        return {k: data[k] for k in fields}, operation["id"]

    def _recover_completion(self, row, operation=None):
        """Recover the Agent's receipt lifetime, including old shortened rows."""
        completion = self._completion_receipt(row, operation)
        if completion is None:
            return row
        data, _ = completion
        expires = data["expires"]
        with self.store.transaction(immediate=True):
            current = self.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
            if (not current or current["upload_id"] != row["upload_id"]
                    or any(current[k] != row[k] for k in _FIELDS)):
                return row
            if current["state"] != "finished" or current["expires"] != expires:
                self.store.execute("""UPDATE incoming_file_imports SET state='finished',expires=?
                    WHERE request_key=?""", (expires, row["request_key"]))
                current = {**current, "state": "finished", "expires": expires}
            return current

    def _record_operation(self, row, operation):
        """Account only authoritative, exact-binding durable outcomes.

        Never deletes or releases Agent spools/staging. Unknown publication,
        storage errors, and interrupted operations retain the recovery manifest.
        A failed chunk RPC can still belong to a perfectly resumable upload.
        """
        if not self._matches_operation(row, operation):
            return
        current = self.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
        if not current or current["state"] in {"finished", "failed"}:
            return
        if operation["state"] == "succeeded":
            self._recover_completion(current, operation)
            return
        state, code = operation["state"], None
        if operation.get("result"):
            try:
                result = json.loads(operation["result"])
                if isinstance(result, dict) and result.get("ok") is False:
                    error = result.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
            except (ValueError, TypeError):
                pass
        if state in {"queued", "running", "reconnecting", "cancelling", "succeeded"}:
            return
        failed = (state == "failed" and (
            operation["tool"] == "incoming_upload_begin" and code in _BEGIN_DENIALS
            or operation["tool"] in {"incoming_upload_status", "incoming_upload_chunk"}
            and code in {"UPLOAD_INTEGRITY", "UPLOAD_EXPIRED"}))
        if not failed and state == "failed" and code in _RECOVERABLE:
            return
        # Even UPLOAD_INTEGRITY from finish can occur after durable staging;
        # only status/chunk can prove the Agent row is definitively failed.
        new_state = "failed" if failed else "recovery_required"
        upload_id = current["upload_id"]
        if operation["tool"] == "incoming_upload_begin":
            if upload_id not in (None, operation["id"]):
                return
            upload_id = operation["id"]
        self.store.execute("""UPDATE incoming_file_imports
            SET state=?,upload_id=?,expires=? WHERE request_key=?""",
            (new_state, upload_id, time.time() + RETENTION if failed else current["expires"], current["request_key"]))

    def record_failure(self, name, args, row, error):
        """An exception alone is insufficient evidence for freeing capacity."""
        identifier = error.details.get("operation_id")
        operation = (self.store.one("SELECT * FROM operations WHERE id=?", (identifier,))
                     if isinstance(identifier, str) and _ID.fullmatch(identifier) else None)
        if operation is None and args.get("idempotency_key"):
            operation = self.store.one("""SELECT * FROM operations
                WHERE space_id=? AND actor=? AND idem=?""",
                (row["space_id"], row["actor"], args["idempotency_key"]))
        if operation and operation["tool"] == name:
            self._record_operation(row, operation)

    def _refresh_owner(self, principal):
        # Late failed begins may be discovered by a later request rather than
        # the original disconnected HTTP waiter. This scan is bounded by quota.
        rows = self.store.all("""SELECT * FROM incoming_file_imports
            WHERE space_id=? AND owner_user_id=? AND state NOT IN ('finished','failed')
            AND (expires>? OR state='recovery_required') ORDER BY created LIMIT ?""",
            (principal.space_id, principal.user_id, time.time(), MAX_GLOBAL_ACTIVE))
        for row in rows:
            operation = self.store.one("""SELECT * FROM operations
                WHERE space_id=? AND actor=? AND idem=?""",
                (row["space_id"], row["actor"], row["begin_key"]))
            self._record_operation(row, operation)

    def cleanup(self):
        """Bounded Hub metadata retention; never touch node files or operations.

        Terminal receipts survive seven days. Expired unfinished imports keep
        seven additional days for reconciliation. Uncertain/active durable work
        is retained for recovery indefinitely instead of forgetting evidence.
        """
        now = time.time()
        removed = retained = 0
        with self.store.transaction(immediate=True):
            rows = self.store.all("""SELECT * FROM incoming_file_imports
                WHERE state!='recovery_required' AND expires<=?
                AND (state IN ('finished','failed') OR expires<=?)
                ORDER BY expires,request_key LIMIT ?""", (now, now - RETENTION, CLEANUP_BATCH))
            for row in rows:
                operations = self.store.all("""SELECT * FROM operations WHERE
                    space_id=? AND actor=? AND project_id=? AND device_id=?
                    AND (idem=? OR (tool IN ('incoming_upload_status','incoming_upload_chunk','incoming_upload_finish')
                        AND CASE WHEN json_valid(args_summary) THEN json_extract(args_summary,'$.upload_id') END=?))
                    AND state!='succeeded' ORDER BY created LIMIT 101""",
                    (row["space_id"], row["actor"], row["project_id"], row["device_id"],
                     row["begin_key"], row["upload_id"]))
                uncertain = len(operations) > 100
                for operation in operations:
                    if not self._matches_operation(row, operation):
                        uncertain = True
                        break
                    if operation["state"] not in {"succeeded", "failed"}:
                        uncertain = True
                        break
                    if row["state"] not in {"finished", "failed"}:
                        self._record_operation(row, operation)
                        updated = self.store.one("SELECT state,expires FROM incoming_file_imports WHERE request_key=?",
                                                 (row["request_key"],))
                        if updated["state"] == "recovery_required" or updated["expires"] > now:
                            uncertain = True
                            break
                if uncertain:
                    latest = self.store.one("SELECT state FROM incoming_file_imports WHERE request_key=?",
                                            (row["request_key"],))
                    if latest["state"] not in {"finished", "failed"}:
                        self.store.execute("UPDATE incoming_file_imports SET state='recovery_required' WHERE request_key=?",
                                           (row["request_key"],))
                    retained += 1
                    continue
                self.store.execute("DELETE FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
                removed += 1
        return {"removed": removed, "retained": retained}

    def reconcile(self, auth, request, row, result, *, begin=False):
        """Bind only the exact durable begin receipt, including pending results."""
        return self.reconcile_for(self.principal(auth, request), row, result, begin=begin)

    def reconcile_for(self, principal, row, result, *, begin=False):
        principal = refresh_principal(self.store, principal)
        current = self.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
        self._check(current, principal)
        identifier = result.get("operation_id") if isinstance(result, dict) else None
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            _error("INVALID_IMPORT_RECEIPT", "Import returned no durable operation receipt", 502)
        upload_id = identifier if begin else current["upload_id"]
        if begin and current["upload_id"] not in (None, identifier):
            _error("IDEMPOTENCY_CONFLICT", "Begin returned a different upload operation", 409)
        if "upload_id" in result and result["upload_id"] != upload_id:
            _error("INVALID_IMPORT_RECEIPT", "Upload receipt identity does not match", 502)
        for field in ("path", "bytes", "sha256"):
            if field in result and result[field] != current[field]:
                _error("INVALID_IMPORT_RECEIPT", "Upload receipt metadata does not match", 502)
        if "received" in result and (type(result["received"]) is not int
                                     or not 0 <= result["received"] <= current["bytes"]):
            _error("INVALID_IMPORT_RECEIPT", "Invalid acknowledged byte offset", 502)
        for field in ("created", "ready", "pending"):
            if field in result and type(result[field]) is not bool:
                _error("INVALID_IMPORT_RECEIPT", "Invalid import state flags", 502)
        if "expires" in result and (type(result["expires"]) not in (int, float)
                or not math.isfinite(result["expires"])
                or not 0 < result["expires"] <= time.time() + 7 * 86400 + 60):
            _error("INVALID_IMPORT_RECEIPT", "Invalid import lifetime", 502)
        complete = result.get("state") == "complete"
        if complete and (result.get("created") is not True or result.get("ready") is not True
                         or result.get("received") != current["bytes"]):
            _error("INVALID_IMPORT_RECEIPT", "Incomplete publication receipt", 502)
        completion_operation_id = None
        with self.store.transaction(immediate=True):
            locked = self.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
            if locked["upload_id"] not in (None, upload_id):
                _error("IDEMPOTENCY_CONFLICT", "Upload identity changed concurrently", 409)
            if locked["state"] == "finished":
                completion = self._completion_receipt(locked)
                if completion is None:
                    _error("INVALID_IMPORT_RECEIPT", "Completed upload needs its original durable finish receipt", 502)
                terminal, completion_operation_id = completion
                # Keep the current step's durable ID (BEGIN must retain the
                # upload ID) while returning separately proven terminal state.
                result = {**terminal, "operation_id": identifier, "pending": False}
                complete = True
            expires = (result.get("expires", locked["expires"]) if complete
                       else min(locked["expires"], result.get("expires", locked["expires"])))
            self.store.execute("UPDATE incoming_file_imports SET upload_id=?,state=?,expires=? WHERE request_key=?",
                (upload_id, "finished" if complete else locked["state"], expires, row["request_key"]))
        # Explicit allowlist: binary payloads, credentials and arbitrary node text
        # must never become HTTP receipts, even if a broken node returns them.
        allowed = ("operation_id", "pending", "state", "retry_after_seconds", "elapsed_seconds",
                   "deadline", "path", "bytes", "received", "sha256", "created", "expires", "ready")
        out = {k: result[k] for k in allowed if k in result}
        out["upload_id"] = upload_id
        if completion_operation_id is not None:
            out["completion_operation_id"] = completion_operation_id
        out.setdefault("pending", False)
        out["next"] = "operations_wait" if out["pending"] else None
        if out["pending"]:
            out["next_call"] = {"tool": "operations_wait", "arguments": {"operation_id": identifier}}
        return out



    def checked_for(self, upload_id, principal):
        if not isinstance(upload_id, str) or not _ID.fullmatch(upload_id):
            _error("FILE_IMPORT_NOT_FOUND", "File import was not found", 404)
        principal = refresh_principal(self.store, principal)
        row = self.store.one("SELECT * FROM incoming_file_imports WHERE upload_id=?", (upload_id,))
        return self._check(row, principal)

    async def _invoke_for(self, name, args, principal, row, *, begin=False):
        if self.remote_inflight >= MAX_INFLIGHT:
            _error("FILE_IMPORT_BUSY", "Too many concurrent import operations", 429)
        self.remote_inflight += 1
        try:
            try:
                result = await self.runtime.invoke(name, args, principal)
            except DevError as exc:
                await run_db(self.store, self.record_failure, name, args, row, exc)
                raise
            try:
                return await run_db(self.store, self.reconcile_for, principal, row, result, begin=begin)
            except DevError as exc:
                identifier = result.get("operation_id") if isinstance(result, dict) else None
                if isinstance(identifier, str) and _ID.fullmatch(identifier):
                    exc.details.setdefault("operation_id", identifier)
                raise
        finally:
            self.remote_inflight -= 1

    async def begin(self, args, principal):
        """For an already authenticated in-process adapter, never a URL fetcher."""
        args = begin_arguments(args)
        row, principal = await run_db(self.store, self.reserve_for, principal, args)
        args["project"] = row["project_id"]
        return await self._invoke_for("incoming_upload_begin", args, principal, row, begin=True)

    async def status(self, upload_id, principal, idempotency_key=None):
        row, principal, _ = await run_db(self.store, self.checked_for, upload_id, principal)
        args = {"project": row["project_id"], "workspace_id": row["workspace_id"], "upload_id": upload_id}
        if idempotency_key is not None:
            args["idempotency_key"] = _text(idempotency_key, "idempotency_key", 8)
        return await self._invoke_for("incoming_upload_status", args, principal, row)

    async def chunk(self, upload_id, offset, raw_bytes, sha256, principal):
        row, principal, _ = await run_db(self.store, self.checked_for, upload_id, principal)
        if (type(offset) is not int or not 0 <= offset < row["bytes"]
                or not isinstance(raw_bytes, bytes) or not 0 < len(raw_bytes) <= CHUNK
                or offset + len(raw_bytes) > row["bytes"]):
            _error("INVALID_IMPORT_CHUNK", "Invalid bounded chunk or byte offset")
        if not isinstance(sha256, str) or not _HEX.fullmatch(sha256) or hashlib.sha256(raw_bytes).hexdigest() != sha256:
            _error("FILE_IMPORT_HASH_MISMATCH", "Chunk bytes do not match SHA-256")
        args = {"project": row["project_id"], "workspace_id": row["workspace_id"],
                "upload_id": upload_id, "offset": offset, "chunk_sha256": sha256,
                "data": base64.b64encode(raw_bytes).decode("ascii"),
                "idempotency_key": _step_key(upload_id, "chunk", offset, sha256)}
        return await self._invoke_for("incoming_upload_chunk", args, principal, row)

    async def finish(self, upload_id, principal):
        row, principal, _ = await run_db(self.store, self.checked_for, upload_id, principal)
        args = {"project": row["project_id"], "workspace_id": row["workspace_id"],
                "upload_id": upload_id, "idempotency_key": _step_key(upload_id, "finish")}
        return await self._invoke_for("incoming_upload_finish", args, principal, row)


def make_incoming_file_router(auth, runtime):
    router = APIRouter()
    service = getattr(runtime, "incoming_files", None)
    if service is None:
        service = runtime.incoming_files = IncomingFileService(runtime)

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    async def body(request, limit, check):
        length = request.headers.get("content-length")
        if length is not None:
            if not re.fullmatch(r"[0-9]{1,12}", length):
                _error("INVALID_CONTENT_LENGTH", "Invalid Content-Length")
            if int(length) > limit:
                _error("FILE_IMPORT_TOO_LARGE", "Request body exceeds the import segment limit", 413)
        if request.headers.get("content-encoding", "identity") != "identity":
            _error("INVALID_IMPORT_ENCODING", "Compressed request bodies are not accepted", 415)
        data = bytearray()
        try:
            async with asyncio.timeout(BODY_TIMEOUT):
                iterator = request.stream().__aiter__()
                while True:
                    await run_db(runtime.store, check)
                    try:
                        segment = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    await run_db(runtime.store, check)
                    if len(data) + len(segment) > limit:
                        _error("FILE_IMPORT_TOO_LARGE", "Request body exceeds the import segment limit", 413)
                    data.extend(segment)
        except TimeoutError as exc:
            raise DevError("FILE_IMPORT_BODY_TIMEOUT", "Timed out receiving import segment", 408) from exc
        if length is not None and len(data) != int(length):
            _error("INVALID_CONTENT_LENGTH", "Actual body length does not match Content-Length")
        return bytes(data)

    async def invoke(request, name, args, row, principal, *, begin=False):
        # Runtime repeats authorization at admission. Post-await checks precede
        # all receipt disclosure; an error is not a claim that nothing happened.
        result = await service._invoke_for(name, args, principal, row, begin=begin)
        # Re-parse the HTTP credential as well as refreshing its original identity.
        await run_db(runtime.store, service.principal, auth, request)
        return response(result)

    @router.post("/api/file-imports")
    async def begin(request: Request):
        await run_db(runtime.store, service.principal, auth, request)
        if service.inflight >= MAX_INFLIGHT:
            _error("FILE_IMPORT_BUSY", "Too many concurrent import requests", 429)
        service.inflight += 1
        try:
            raw = await body(request, MAX_JSON, lambda: service.principal(auth, request))
            try:
                args = begin_arguments(json.loads(raw))
            except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                raise DevError("INVALID_IMPORT", "Expected bounded UTF-8 JSON metadata") from exc
            row, principal = await run_db(runtime.store, service.reserve, auth, request, args)
            args["project"] = row["project_id"]
            return await invoke(request, "incoming_upload_begin", args, row, principal, begin=True)
        finally:
            service.inflight -= 1

    @router.get("/api/file-imports/{upload_id}")
    async def status(upload_id: str, request: Request):
        row, principal, _ = await run_db(runtime.store, service.checked, auth, request, upload_id)
        args = {"project": row["project_id"], "workspace_id": row["workspace_id"], "upload_id": upload_id}
        # Status must be fresh; a caller may provide a key only to recover this
        # exact status operation after an uncertain response.
        key = request.query_params.get("idempotency_key")
        if key is not None:
            args["idempotency_key"] = _text(key, "idempotency_key", 8)
        return await invoke(request, "incoming_upload_status", args, row, principal)

    @router.put("/api/file-imports/{upload_id}/chunks")
    async def chunk(upload_id: str, request: Request):
        row, principal, _ = await run_db(runtime.store, service.checked, auth, request, upload_id)
        raw_offset = request.query_params.get("offset", "")
        if not re.fullmatch(r"[0-9]{1,10}", raw_offset):
            _error("INVALID_IMPORT_OFFSET", "Expected a nonnegative byte offset")
        offset = int(raw_offset)
        digest = request.headers.get("x-chunk-sha256", "")
        if not _HEX.fullmatch(digest):
            _error("INVALID_IMPORT_HASH", "Expected X-Chunk-Sha256")
        if offset >= row["bytes"]:
            _error("INVALID_IMPORT_OFFSET", "Chunk starts outside the declared file")
        if service.inflight >= MAX_INFLIGHT:
            _error("FILE_IMPORT_BUSY", "Too many concurrent import requests", 429)
        service.inflight += 1
        try:
            raw = await body(request, min(CHUNK, row["bytes"] - offset),
                             lambda: service.checked(auth, request, upload_id))
            if not raw or hashlib.sha256(raw).hexdigest() != digest:
                _error("FILE_IMPORT_HASH_MISMATCH", "Chunk bytes do not match X-Chunk-Sha256")
            row, principal, _ = await run_db(runtime.store, service.checked, auth, request, upload_id)
            args = {"project": row["project_id"], "workspace_id": row["workspace_id"],
                    "upload_id": upload_id, "offset": offset, "chunk_sha256": digest,
                    "data": base64.b64encode(raw).decode("ascii"),
                    "idempotency_key": _step_key(upload_id, "chunk", offset, digest)}
            return await invoke(request, "incoming_upload_chunk", args, row, principal)
        finally:
            service.inflight -= 1

    @router.post("/api/file-imports/{upload_id}/finish")
    async def finish(upload_id: str, request: Request):
        row, principal, _ = await run_db(runtime.store, service.checked, auth, request, upload_id)
        # No file bytes or user-selected destination are accepted at commit.
        await body(request, 0, lambda: service.checked(auth, request, upload_id))
        row, principal, _ = await run_db(runtime.store, service.checked, auth, request, upload_id)
        args = {"project": row["project_id"], "workspace_id": row["workspace_id"],
                "upload_id": upload_id, "idempotency_key": _step_key(upload_id, "finish")}
        return await invoke(request, "incoming_upload_finish", args, row, principal)

    return router
