"""Native fileParams adapter to the same authenticated resumable byte ingress.

The host's file_id is not an authenticity proof. URL fetching remains a separate,
explicitly configured Hub source policy. No browser credentials are forwarded.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time

from agent.incoming_artifacts import download_chunks, validate_url
from hub.db_worker import run_db
from hub.incoming_files import CHUNK, IncomingFileService, ingress_enabled, _request_key, _step_key
from hub.core_tools import validate
from shared.file_sources import (
    file_source_policy, normalize_file_hosts, normalize_file_source_providers,
    safe_import_error_detail, source_metadata,
)
from shared.util import DevError

MAX_ACTIVE = 4
TRANSFER_SECONDS = 300


def native_ingress_enabled():
    return ingress_enabled() and os.environ.get("CODEPIER_NATIVE_FILE_RELAY", "") == "true"


def native_source_policy():
    """Unconfigured profiles remain off; malformed deployment settings fail closed."""
    config = {}
    try:
        if "CODEPIER_NATIVE_FILE_HOSTS" in os.environ:
            config["file_hosts"] = normalize_file_hosts(
                json.loads(os.environ["CODEPIER_NATIVE_FILE_HOSTS"]), "file_hosts")
        if "CODEPIER_NATIVE_FILE_PROVIDERS" in os.environ:
            config["file_source_providers"] = normalize_file_source_providers(
                json.loads(os.environ["CODEPIER_NATIVE_FILE_PROVIDERS"]))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise DevError("FILE_IMPORT_POLICY_INVALID", "Hub native-file source policy is invalid", 503) from None
    return {**file_source_policy(config), "policy_scope": "hub"}


def _service(runtime):
    if not native_ingress_enabled():
        raise DevError("FILE_IMPORT_DISABLED", "Native-file relay is not enabled", 403)
    service = getattr(runtime, "incoming_files", None)
    if service is None:
        service = runtime.incoming_files = IncomingFileService(runtime)
    return service


def _target(service, args, principal):
    principal, project = service.project(args["project"], principal)
    if not service.runtime.online(project["device_id"]):
        raise DevError("FILE_IMPORT_DEVICE_OFFLINE",
                       "Target node is offline; no source file was fetched. Resume on the same node.", 503)
    device = service.store.one("SELECT info FROM devices WHERE id=?", (project["device_id"],))
    try:
        info = json.loads(device["info"]) if device else {}
        capabilities = info.get("capabilities", []) if isinstance(info, dict) else []
    except (ValueError, TypeError):
        capabilities = []
    if not isinstance(capabilities, list) or "incoming_upload_begin" not in capabilities:
        raise DevError("AGENT_UPGRADE_REQUIRED",
                       "Target node has not advertised resumable file ingress; no source file was fetched.", 409)
    return principal, project


def _hub_source_error(exc):
    if exc.code == "ARTIFACT_SOURCE_DENIED":
        details = safe_import_error_detail(exc.details)
        if details.get("reason") == "host_not_allowed":
            details["recovery"] = "review_hub_file_sources"
        message = ("Hub native-file source was denied; review CODEPIER_NATIVE_FILE_HOSTS or "
                   "CODEPIER_NATIVE_FILE_PROVIDERS with the owner. Do not change node policy or disable validation."
                   if details.get("reason") == "host_not_allowed"
                   else "Hub native-file source validation failed; no denied destination was requested.")
        return DevError(exc.code, message, exc.status, **details)
    return exc


async def inspect_native_file(runtime, raw, principal):
    args = validate("inspect_file_source", raw).model_dump()
    service = _service(runtime)
    await run_db(service.store, runtime.project, args["project"], principal)
    policy = native_source_policy()
    file = args["file"]
    size = file.get("size")
    result = {**policy, **source_metadata(file["download_url"]),
        "checked": True, "source_allowed": False, "declared_size": size,
        "size_allowed": None if size is None else size <= policy["max_bytes"],
        "request_sent": False, "created": False, "host_roundtrip": "not_run",
        "approval_required": False,
        "note": "Hub source-policy preview only; no source request, DNS/TLS or file-content validation occurred."}
    try:
        validate_url(file["download_url"], policy["allowed_hosts"],
                     providers=policy["file_source_providers"])
    except DevError as exc:
        result.update(safe_import_error_detail(exc.details))
        result["message"] = ("Hub download source is not approved; review the exact source or named "
                             "provider with the owner before changing the Hub policy.")
        if exc.details.get("reason") == "host_not_allowed":
            result["approval_required"] = True
            result["approval_target"] = {"config_key": "CODEPIER_NATIVE_FILE_HOSTS",
                "hosts": [result["source_host"]], "scope": "hub",
                "applies_to": "native_file_ingress_on_this_hub"}
    else:
        result["source_allowed"] = True
    return result


async def _resolved(service, receipt, principal, deadline):
    """Wait for the same operation, never replay a mutating step with a new ID."""
    value = receipt
    while value.get("pending") is True:
        identifier = value["operation_id"]
        if time.monotonic() >= deadline:
            raise DevError("FILE_IMPORT_PENDING",
                           "Import is still pending; recover the original operation before retrying.",
                           409, operation_id=identifier, upload_id=value.get("upload_id"))
        await service.runtime.wait_operation(identifier, principal, 10)
        def read():
            row, current, _ = service.checked_for(value["upload_id"], principal)
            operation = service.runtime.operation(identifier, current, {"include_output": False})
            if operation.get("pending"):
                return value
            result = operation.get("result") or {}
            if result.get("ok") is not True:
                error = result.get("error") or {}
                raise DevError(error.get("code", "FILE_IMPORT_FAILED"),
                               "The original file import operation failed; inspect its saved receipt.",
                               409, operation_id=identifier, upload_id=row["upload_id"])
            data = {**(result.get("data") or {}), "operation_id": identifier, "pending": False}
            return service.reconcile_for(current, row, data)
        value = await run_db(service.store, read)
    return value



def _reserve_native(service, args, principal):
    """Bind native intent independently of short-lived signed URL text."""
    principal, project = service.project(args["project"], principal)
    file = args["file"]
    source = (["expected_sha256", args["expected_sha256"]] if args.get("expected_sha256")
              else ["native_file_id_sha256", hashlib.sha256(file["file_id"].encode()).hexdigest()])
    identity = hashlib.sha256(json.dumps({
        "source": source, "project": project["id"], "device": project["device_id"],
        "root": project["root"], "workspace": args["workspace_id"], "path": args["path"],
        "size": file.get("size"),
    }, sort_keys=True).encode()).hexdigest()
    key = _request_key(principal, args["idempotency_key"])
    service.store.execute("""CREATE TABLE IF NOT EXISTS native_file_ingress_requests (
        request_key TEXT PRIMARY KEY, identity TEXT NOT NULL, created REAL NOT NULL)""")
    with service.store.transaction(immediate=True):
        old = service.store.one("SELECT identity FROM native_file_ingress_requests WHERE request_key=?", (key,))
        if old and old["identity"] != identity:
            raise DevError("IDEMPOTENCY_CONFLICT", "Native import key identifies a different file or destination", 409)
        if not old:
            if service.store.one("SELECT 1 FROM incoming_file_imports WHERE request_key=?", (key,)):
                raise DevError("IDEMPOTENCY_CONFLICT", "Native source identity is unavailable for this existing import key", 409)
            now = time.time()
            # Manifest cleanup owns terminal/recovery retention. A stale active
            # expiry can still have a durable finish awaiting reconciliation.
            service.store.execute("""DELETE FROM native_file_ingress_requests WHERE created<?
                AND request_key NOT IN (SELECT request_key FROM incoming_file_imports)""",
                                  (now - 7 * 86400,))
            if service.store.one("SELECT COUNT(*) AS n FROM native_file_ingress_requests")["n"] >= 4096:
                raise DevError("FILE_IMPORT_BUSY", "Native import metadata quota reached", 429)
            service.store.execute("INSERT INTO native_file_ingress_requests VALUES(?,?,?)",
                                  (key, identity, time.time()))
    manifest = service.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (key,))
    if not manifest or not manifest["upload_id"]:
        return principal, project, None
    service._check(manifest, principal, project=project)
    if (manifest["path"] != args["path"] or manifest["workspace_id"] != args["workspace_id"]
            or file.get("size") is not None and file["size"] != manifest["bytes"]
            or args.get("expected_sha256") and args["expected_sha256"] != manifest["sha256"]):
        raise DevError("IDEMPOTENCY_CONFLICT", "Native import metadata differs from its original manifest", 409)
    operation = service.store.one(
        "SELECT id,result FROM operations WHERE space_id=? AND actor=? AND idem=? AND tool=?",
        (principal.space_id, principal.actor, _step_key(manifest["upload_id"], "finish"), "incoming_upload_finish"))
    if operation and operation["result"]:
        result = service.runtime.authorized_result(operation["id"], json.loads(operation["result"]), principal)
        result = service.reconcile_for(principal, manifest,
            {**result, "operation_id": operation["id"], "pending": False})
        if result.get("state") == "complete":
            _verify_final(result, manifest["path"], manifest["bytes"], manifest["sha256"])
            return principal, project, {**result, "overwritten": False, "extracted": False,
                "executed": False, "transfer": "authenticated_hub_ingress", "recovered": True}
    return principal, project, None


def _verify_final(result, path, size, sha):
    if (result.get("created") is not True or result.get("ready") is not True
            or result.get("pending") is True or result.get("state") != "complete"
            or result.get("bytes") != size or result.get("received") != size
            or result.get("sha256") != sha or result.get("path") != path):
        raise DevError("INVALID_IMPORT_RECEIPT", "Final import verification failed", 502)


async def _finish_writer(work):
    """Hold file lifetime and quota until the thread really stops, even if cancelled twice."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(work)
            break
        except asyncio.CancelledError:
            cancelled = True
            if work.cancelled():
                raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def import_native_file(runtime, raw, principal):
    args = validate("download_artifact", raw).model_dump()
    service = _service(runtime)
    principal, project, recovered = await run_db(service.store, _reserve_native, service, args, principal)
    if recovered is not None:
        return recovered
    principal, project = await run_db(service.store, _target, service, args, principal)
    policy = native_source_policy()
    file = args["file"]
    try:
        validate_url(file["download_url"], policy["allowed_hosts"],
                     providers=policy["file_source_providers"])
    except DevError as exc:
        raise _hub_source_error(exc) from None
    if file.get("size") is not None and file["size"] > policy["max_bytes"]:
        raise DevError("ARTIFACT_TOO_LARGE", "Native attachment exceeds the Hub transfer limit")
    active = getattr(runtime, "_native_ingress_active", 0)
    if active >= MAX_ACTIVE:
        raise DevError("FILE_IMPORT_BUSY", "Native-file transfer capacity is full; retry later", 429)
    runtime._native_ingress_active = active + 1
    deadline = time.monotonic() + TRANSFER_SECONDS
    try:
        # Anonymous/deletion-on-close temporary storage is bounded by MAX_ACTIVE
        # and max_bytes. It is never an offline durable acceptance guarantee.
        with tempfile.TemporaryFile(mode="w+b", dir=service.store.directory) as buffer:
            def fetch():
                total, digest = 0, hashlib.sha256()
                for block in download_chunks(file, policy["allowed_hosts"], policy["max_bytes"],
                                             providers=policy["file_source_providers"]):
                    # Refresh the original grant during a long source download.
                    service.project(project["id"], principal)
                    total += len(block)
                    buffer.write(block)
                    digest.update(block)
                if file.get("size") is not None and file["size"] != total:
                    raise DevError("ARTIFACT_SIZE", "Native attachment size did not match")
                sha = digest.hexdigest()
                if args.get("expected_sha256") and sha != args["expected_sha256"]:
                    raise DevError("ARTIFACT_INTEGRITY", "Native attachment SHA-256 did not match")
                buffer.flush()
                return total, sha

            work = asyncio.create_task(asyncio.to_thread(fetch))
            size, sha = await _finish_writer(work)
            principal, current = await run_db(service.store, _target, service, args, principal)
            if (current["id"], current["device_id"], current["root"]) != (
                    project["id"], project["device_id"], project["root"]):
                raise DevError("FILE_IMPORT_MAPPING_CHANGED", "Project changed during source transfer", 409)
            begin = await service.begin({"project": project["id"], "workspace_id": args["workspace_id"],
                "idempotency_key": args["idempotency_key"], "path": args["path"],
                "size": size, "sha256": sha}, principal)
            begin = await _resolved(service, begin, principal, deadline)
            upload_id = begin["upload_id"]
            status = (begin if begin.get("created") is True else
                      await _resolved(service, await service.status(upload_id, principal), principal, deadline))
            if status.get("state") == "complete":
                final = status
            else:
                offset = status["received"]
                buffer.seek(offset)
                while offset < size:
                    if time.monotonic() >= deadline:
                        raise DevError("FILE_IMPORT_PENDING", "Import transfer paused; resume the same upload",
                                       409, upload_id=upload_id)
                    data = buffer.read(min(CHUNK, size - offset))
                    if not data:
                        raise DevError("ARTIFACT_INTEGRITY", "Native temporary file became incomplete")
                    step = await service.chunk(upload_id, offset, data, hashlib.sha256(data).hexdigest(), principal)
                    step = await _resolved(service, step, principal, deadline)
                    if step.get("created") is True:
                        _verify_final(step, args["path"], size, sha)
                        return {**step, "overwritten": False, "extracted": False, "executed": False,
                                "transfer": "authenticated_hub_ingress"}
                    if step.get("received") != offset + len(data):
                        raise DevError("INVALID_IMPORT_RECEIPT", "Unexpected acknowledged import offset", 502)
                    offset += len(data)
                final = await _resolved(service, await service.finish(upload_id, principal), principal, deadline)
            _verify_final(final, args["path"], size, sha)
            return {**final, "overwritten": False, "extracted": False, "executed": False,
                    "transfer": "authenticated_hub_ingress"}
    except DevError as exc:
        raise _hub_source_error(exc) from None
    except OSError:
        raise DevError('FILE_IMPORT_STORAGE', 'Hub temporary file storage failed; inspect the original upload receipt', 503) from None
    finally:
        runtime._native_ingress_active -= 1
