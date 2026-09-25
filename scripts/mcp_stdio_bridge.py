"""Private stdio -> authenticated HTTP MCP bridge with bounded transport retries.

One input call gets one stable key across all HTTP attempts. stdout contains only
JSON-RPC; credentials are read from a private local file and never logged.
"""
from __future__ import annotations

import copy
import json
import math
import os
import random
import re
import stat
import sys
import time
import uuid
from pathlib import Path

import httpx

from shared.util import normalize_url, valid_json_value
from shared.contracts import TOOLS
from shared.mcp_protocol import MODERN, is_modern, request_headers

MAX_LINE = 6 * 1024 * 1024
VERSIONS = {"2025-03-26", "2025-06-18", "2025-11-25"}
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
# Derive capabilities from the same registry as Hub validation and MCP schemas.
# Some Hub-local tools mutate progress, so local does NOT imply keyless/read-only.
REMOTE_TOOLS = {name for name, tool in TOOLS.items() if not tool.local}
IDEMPOTENT_TOOLS = {name for name, tool in TOOLS.items() if "idempotency_key" in tool.model.model_fields}



def token_from_file(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("Token file must not be a symlink")
    # Inspect and read the same descriptor. A path replacement between stat and
    # read must not bypass permission/type checks or make us block on a FIFO.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2048:
            raise ValueError("Token file must be a small regular file")
        if os.name != "nt" and info.st_mode & 0o077:
            raise ValueError("Token file permissions must be 0600 (chmod 600 token.txt)")
        raw = os.read(descriptor, 2049)
        if len(raw) > 2048:
            raise ValueError("Token file must be a small regular file")
        value = raw.decode("utf-8").strip()
    finally:
        os.close(descriptor)
    if re.fullmatch(r"rd_[A-Za-z0-9_-]{32,497}", value) is None:
        raise ValueError("Expected a scoped personal access token from the panel")
    return value


def reject_constant(value):
    raise ValueError("Non-finite values are not JSON")


def valid_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def emit_error(id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    print(json.dumps({"jsonrpc": "2.0", "id": id, "error": error}, ensure_ascii=True), flush=True)


def validate_response(payload, request):
    """Reject corrupt envelopes before they can strand an RPC or poison a session."""
    if (not isinstance(payload, dict) or not valid_json_value(payload) or payload.get("jsonrpc") != "2.0"
            or "id" not in payload or type(payload["id"]) is not type(request.get("id"))
            or payload["id"] != request.get("id")
            or ("result" in payload) == ("error" in payload)):
        raise ValueError("Invalid/corrupted MCP response")
    if "error" in payload:
        error = payload["error"]
        if (not isinstance(error, dict) or not isinstance(error.get("code"), int)
                or isinstance(error["code"], bool) or not isinstance(error.get("message"), str)):
            raise ValueError("Invalid MCP error response")
        return
    result = payload["result"]
    if not isinstance(result, dict):
        raise ValueError("Invalid MCP result")
    if request.get("method") == "initialize":
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in VERSIONS:
            raise ValueError("Invalid MCP protocol version")
    if is_modern(request) and result.get("resultType") != "complete":
        raise ValueError("Invalid modern MCP CompleteResult")
    if request.get("method") == "server/discover":
        versions = result.get("supportedVersions")
        if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions) or not isinstance(result.get("capabilities"), dict):
            raise ValueError("Invalid MCP discovery response")
    if request.get("method") == "tools/list":
        catalog = result.get("tools")
        if not isinstance(catalog, list) or any(
                not isinstance(tool, dict) or not isinstance(tool.get("_meta", {}), dict)
                for tool in catalog):
            raise ValueError("Invalid MCP tool catalog")


def prepare_request(request):
    prepared = copy.deepcopy(request)
    params = prepared.get("params", {})
    if (prepared.get("method") == "tools/call" and isinstance(params, dict)
            and isinstance(params.get("name"), str) and params["name"] in IDEMPOTENT_TOOLS):
        args = params.setdefault("arguments", {})
        if params['name'] == 'process' and isinstance(args, dict) and args.get('operation', 'list') not in {'search_start', 'search_cancel', 'validate'} or params['name'] == 'workspace' and isinstance(args, dict) and args.get('operation', 'list') in {'list', 'help'}:
            return prepared
        if isinstance(args, dict) and (args.get("idempotency_key") is None or args.get("idempotency_key") == ""):
            args["idempotency_key"] = "bridge-" + uuid.uuid4().hex
    return prepared


def forward(client, base, request, headers, *, retry_seconds=30, sleep=time.sleep, clock=time.monotonic, profile="core"):
    """Retry only transport failures, never a tool's SHA/permission/test error.

    request must already be prepared; generating keys inside this loop would
    duplicate writes/tests after a lost response.
    """
    if profile not in {"core", "full", "coding"}:
        raise ValueError("MCP profile must be core, full or coding")
    endpoint = base + "/mcp" + ("?profile=" + profile if profile != "core" else "")
    deadline = clock() + retry_seconds
    attempt = 0
    while True:
        attempt += 1
        response = None
        wire = copy.deepcopy(request) if is_modern(request) else request
        wire_headers = dict(headers)
        if is_modern(request):
            # Fresh transport ID per HTTP attempt; the prepared tool key stays fixed.
            if "id" in wire: wire["id"] = "codepier-http-" + uuid.uuid4().hex
            wire_headers.update(request_headers(wire))
        try:
            left = max(.1, deadline - clock())
            response = client.post(endpoint, headers=wire_headers, json=wire,
                                   timeout=httpx.Timeout(min(12, left), connect=min(4, left)))
            if response.status_code in (202, 204) and "id" not in request:
                return None
            if response.status_code in {400, 404} and "id" in request:
                try:
                    protocol_error = response.json(parse_constant=reject_constant)
                    validate_response(protocol_error, wire)
                    if "error" in protocol_error:
                        protocol_error["id"] = request["id"]
                        return protocol_error
                except (ValueError, RecursionError):
                    pass
            response.raise_for_status()
            if "id" not in request:
                return None
            payload = response.json(parse_constant=reject_constant)
            validate_response(payload, wire)
            payload["id"] = request["id"]
            return payload
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError, RecursionError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if status is not None and status not in RETRY_STATUSES:
                raise
            remaining = deadline - clock()
            if remaining <= 0 or attempt >= 6:
                raise
            delay = min(4, .5 * (2 ** (attempt - 1))) + random.uniform(0, .2)
            if response is not None:
                try:
                    retry_after = float(response.headers.get("retry-after", "0"))
                    if math.isfinite(retry_after):
                        delay = max(delay, min(5, retry_after))
                except ValueError:
                    pass
            print(f"Hub temporarily unavailable; retry {attempt}/6 with the SAME operation key.", file=sys.stderr, flush=True)
            sleep(min(delay, remaining))
            if clock() >= deadline:
                raise


def main():
    for suffix in ('HUB_URL','TOKEN_FILE','MCP_PROFILE','RETRY_SECONDS'):
        new, old = 'CODEPIER_'+suffix, 'REMOTE_DEV_'+suffix
        if new not in os.environ and old in os.environ: os.environ[new] = os.environ[old]
    try:
        base = normalize_url(os.environ.get("CODEPIER_HUB_URL", "http://127.0.0.1:8765"))
        profile = os.environ.get("CODEPIER_MCP_PROFILE", "core")
        if profile not in {"core", "full", "coding"}:
            raise ValueError("CODEPIER_MCP_PROFILE must be core, full or coding")
        token_file = Path(os.environ["CODEPIER_TOKEN_FILE"]).expanduser()
        token_from_file(token_file)
        retry_seconds = float(os.environ.get("CODEPIER_RETRY_SECONDS", "30"))
        if not math.isfinite(retry_seconds):
            raise ValueError("Retry duration must be finite")
        retry_seconds = max(1, min(retry_seconds, 120))
    except (KeyError, ValueError, OSError):
        print("Bridge requires CODEPIER_TOKEN_FILE with a valid private PAT; check CODEPIER_HUB_URL.", file=sys.stderr)
        return 2
    version = "2025-11-25"
    with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
        while True:
            raw = sys.stdin.buffer.readline(MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE:
                print("MCP input line exceeds 6 MiB; stopping without forwarding.", file=sys.stderr)
                return 2
            if not raw.strip():
                continue
            try:
                request = json.loads(raw, parse_constant=reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                emit_error(None, -32700, "Parse error")
                continue
            if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                    or not isinstance(request.get("method"), str)
                    or ("id" in request and not valid_id(request["id"]))
                    or not valid_json_value(request)):
                emit_error(None, -32600, "Invalid JSON-RPC request")
                continue
            params = request.get("params", {})
            if (not isinstance(params, dict) or (request["method"] == "tools/call" and (
                    not isinstance(params.get("name"), str)
                    or not isinstance(params.get("arguments", {}), dict)))):
                if "id" in request:
                    emit_error(request["id"], -32602, "Expected params and tool arguments objects")
                continue
            try:
                token = token_from_file(token_file)
            except (ValueError, OSError):
                print("Bridge token unavailable; check the private PAT file.", file=sys.stderr)
                if "id" in request:
                    emit_error(request["id"], -32001, "Bridge token unavailable; check the private PAT file",
                               {"retryable": False, "forwarded": False})
                continue
            try:
                request = prepare_request(request)
                headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                           "Authorization": "Bearer " + token, "MCP-Protocol-Version": version}
                payload = forward(client, base, request, headers, retry_seconds=retry_seconds, profile=profile)
                if payload is None:
                    continue
                if request["method"] == "initialize" and "result" in payload:
                    version = payload["result"]["protocolVersion"]
                if request["method"] == "tools/list" and "result" in payload:
                    for tool in payload["result"]["tools"]:
                        tool.setdefault("_meta", {}).pop("securitySchemes", None)
                        tool.pop("securitySchemes", None)
                if request["method"] == "tools/call" and isinstance(payload.get("result"), dict):
                    # A private stdio PAT is not an OAuth client; do not launch host OAuth.
                    payload["result"].get("_meta", {}).pop("mcp/www_authenticate", None)
                if "id" in request:
                    print(json.dumps(payload, ensure_ascii=True), flush=True)
            except Exception as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                params = request.get("params", {}) if isinstance(request, dict) else {}
                arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
                key = arguments.get("idempotency_key") if isinstance(arguments, dict) else None
                workflow_id = arguments.get("workflow_id") if isinstance(arguments, dict) else None
                workflow_call = isinstance(params.get("name"), str) and params["name"].startswith("workflows_")
                recovery_tool = ("workflows_get" if workflow_id else "workflows_list") if workflow_call else "operations_list"
                message = "Hub authorization rejected; renew the bridge PAT" if status in (401, 403) else f"Transport retries exhausted. The request may already be saved. Recover via {recovery_tool} or resend the identical arguments with the SAME idempotency_key; never start another mutation/task with a new key."
                print(f"Bridge failure: {type(exc).__name__}; HTTP={status}", file=sys.stderr)
                if isinstance(request, dict) and "id" in request:
                    emit_error(request["id"], -32000, message,
                               {"retryable": status is None or status in RETRY_STATUSES,
                                "idempotency_key": key, "next": recovery_tool, **({"workflow_id": workflow_id} if workflow_call and workflow_id else {})})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
