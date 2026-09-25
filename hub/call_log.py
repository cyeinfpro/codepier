"""Administrative, read-only projections of durable tool operations.

Never decrypt the queued request to display a log, and never run an operation
when expanding its entry. Historical records retain their original evidence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time

from fastapi import Query, Request

from hub.db_worker import database_endpoint
from shared.audit_redaction import display_value, redact_text

TERMINAL = {"succeeded", "failed", "cancelled", "needs_review", "interrupted"}
STATES = TERMINAL | {"queued", "running", "reconnecting", "cancelling", "unknown"}


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def argument_summary(args: dict) -> str:
    parts = []
    for key in ("path", "command", "task", "query", "cwd", "start_line", "max_lines", "offset", "timeout_seconds"):
        value = args.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value != "":
            parts.append(f"{key}={str(value).replace(chr(10), ' ↵ ')[:600]}")
    paths = args.get("paths")
    if isinstance(paths, list):
        parts.append("paths=" + ", ".join(str(p)[:120] for p in paths[:3]))
        parts.append(f"files={len(paths)}{'+' if len(paths) >= 30 else ''}")
    meta = args.get("_log") if isinstance(args.get("_log"), dict) else {}
    edits = meta.get("edit_count")
    if isinstance(edits, int) and edits >= 0:
        parts.append(f"edits={edits}")
    elif isinstance(args.get("edits"), list):
        count = len(args["edits"])
        parts.append(f"edits={count}{'+' if count >= 30 else ''}")
    content = args.get("content")
    match = re.fullmatch(r"<(\d+) chars>", content) if isinstance(content, str) else None
    if match:
        parts.append("inputChars=" + match[1])
    chars = meta.get("command_chars")
    if isinstance(chars, int) and chars >= 0:
        parts.append(f"commandChars={chars}")
    return "  ".join(parts)[:1200]


def operation_summary(args: dict) -> dict:
    """Persist counts alongside the existing privacy-preserving summary."""
    from shared.util import safe_summary
    result = safe_summary(args)
    meta = {"version": 1}
    if isinstance(args.get("command"), str):
        meta["command_chars"] = len(args["command"])
    if isinstance(args.get("edits"), list):
        meta["edit_count"] = len(args["edits"])
    result["_log"] = meta
    return result


def public_row(row: dict, now: float | None = None) -> dict:
    public = {"id", "project_id", "device_id", "tool", "actor", "state", "created", "updated", "args_summary", "error", "attempts", "alias", "device_name", "accepted_at", "deadline", "cancel_requested", "transport_error"}
    result = {key: value for key, value in row.items() if key in public}
    args, clipped, scrubbed = display_value(_object(result.get("args_summary")), text_limit=1200, budget=12000)
    result["args_summary"] = args
    result["summary"] = argument_summary(args)
    result["arguments_truncated"] = clipped or "…" in json.dumps(args, ensure_ascii=False)
    created = result.get("created")
    end = result.get("updated") if result.get("state") in TERMINAL else (time.time() if now is None else now)
    valid_times = all(type(value) in (float, int) and math.isfinite(value) for value in (created, end))
    result["elapsed_ms"] = max(0, round((end - created) * 1000)) if valid_times else None
    result["exit_code"] = None
    result, more_clipped, more_scrubbed = display_value(result, text_limit=1600, budget=16000)
    result["display"] = {"truncated": clipped or more_clipped, "redacted": scrubbed or more_scrubbed}
    result.pop("args_summary", None)
    return result


def trace_timing(trace: dict, created) -> dict:
    """Pair monotonic Agent offsets only inside a witnessed handling attempt."""
    wait = execution = None
    accepted = start = None
    for event in trace.get("events", []):
        if not isinstance(event, dict) or event.get("source") != "agent":
            continue
        stage = event.get("stage")
        elapsed = event.get("elapsed_ms")
        seq = event.get("seq")
        if stage == "accepted":
            accepted = event
            start = None
            execution = None
        elif stage == "executing":
            observed = event.get("at")
            if wait is None and isinstance(observed, (int, float)) and isinstance(created, (int, float)) and observed >= created:
                wait = round((observed - created) * 1000)
            if accepted is not None:
                start = event
        elif stage == "persisting" and start is not None:
            begin = start.get("elapsed_ms")
            first_seq = start.get("seq")
            if type(elapsed) is int and type(begin) is int and elapsed >= begin and type(seq) is int and type(first_seq) is int and seq > first_seq:
                execution = elapsed - begin
            start = None
    return {"wait_ms": wait, "execution_ms": execution}


def _filter_id(filters):
    return hashlib.sha256(json.dumps(filters, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def _cursor(created, identifier, filters):
    return base64.urlsafe_b64encode(json.dumps([1, created, identifier, _filter_id(filters)], separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor, filters):
    from shared.util import DevError
    try:
        raw = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        version, created, identifier, fingerprint = raw
        if version != 1 or isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created) or created < 0:
            raise ValueError()
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 100 or fingerprint != _filter_id(filters):
            raise ValueError()
        return created, identifier
    except (ValueError, TypeError, OverflowError, UnicodeError) as exc:
        raise DevError("INVALID_CURSOR", "调用列表游标无效或筛选已变化，请回到第一页") from exc


def register_call_log(app, runtime, auth):
    from shared.util import DevError
    store = runtime.store

    @app.get("/api/call-log")
    @database_endpoint(store)
    def call_log(request: Request, limit: int = Query(40, ge=1, le=100), cursor: str = Query("", max_length=1024), q: str = Query("", max_length=200), source: str = Query("", max_length=20), status: str = Query("", max_length=30), project: str = Query("", max_length=100), tool: str = Query("", max_length=100), watch: str = Query("", max_length=4096), include_filters: bool = True):
        auth.admin(request)
        if source not in {"", "mcp", "panel"} or status not in STATES | {""}:
            raise DevError("INVALID_FILTER", "无效的调用来源或状态")
        filters = {"q": q.strip(), "source": source, "status": status, "project": project, "tool": tool}
        where, values = [], []
        for column, value in (("o.state", status), ("o.project_id", project), ("o.tool", tool)):
            if value:
                where.append(column + "=?")
                values.append(value)
        if source:
            where.append("o.actor LIKE ?")
            values.append(source + ":%")
        if filters["q"]:
            literal = filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            columns = ("o.id", "o.actor", "o.tool", "o.args_summary", "o.error", "p.alias", "d.name")
            where.append("(" + " OR ".join(column + " LIKE ? ESCAPE '\\'" for column in columns) + ")")
            values.extend(["%" + literal + "%"] * len(columns))
        if cursor:
            created, identifier = _decode_cursor(cursor, filters)
            where.append("(o.created < ? OR (o.created = ? AND o.id < ?))")
            values.extend([created, created, identifier])
        # The list never loads encrypted payloads, result bodies or task output.
        sql = "SELECT o.id,o.project_id,o.device_id,o.tool,o.actor,o.state,o.created,o.updated,o.args_summary,o.error,o.attempts,p.alias,d.name AS device_name FROM operations o LEFT JOIN projects p ON p.id=o.project_id LEFT JOIN devices d ON d.id=o.device_id"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = store.all(sql + " ORDER BY o.created DESC,o.id DESC LIMIT ?", (*values, limit + 1))
        more = len(rows) > limit
        rows = rows[:limit]
        observed = time.time()
        watched = list(dict.fromkeys(watch.split(","))) if watch else []
        if len(watched) > 40 or any(not re.fullmatch(r"[a-f0-9]{32}", x) for x in watched):
            raise DevError("INVALID_FILTER", "无效的调用状态订阅")
        updates = []
        if watched:
            base = "SELECT o.id,o.project_id,o.device_id,o.tool,o.actor,o.state,o.created,o.updated,o.args_summary,o.error,o.attempts,p.alias,d.name AS device_name FROM operations o LEFT JOIN projects p ON p.id=o.project_id LEFT JOIN devices d ON d.id=o.device_id"
            updates = store.all(base + " WHERE o.id IN (" + ",".join("?" for _ in watched) + ")", tuple(watched))
        return {"updates": [public_row(row, observed) for row in updates], "operations": [public_row(row, observed) for row in rows], "next_cursor": _cursor(rows[-1]["created"], rows[-1]["id"], filters) if more else None, "observed_at": observed, "projects": store.all("SELECT id,alias FROM projects ORDER BY alias,id") if include_filters else [], "tools": [x["tool"] for x in store.all("SELECT DISTINCT tool FROM operations ORDER BY tool LIMIT 200")] if include_filters else []}

    @app.get("/api/call-log/{identifier}")
    @database_endpoint(store)
    def call_log_detail(identifier: str, request: Request):
        principal = auth.admin(request)
        original = runtime.operation(identifier, principal)
        row = public_row({key: value for key, value in original.items() if key not in {"result", "output"}})
        sections = {}
        any_clipped = any_scrubbed = False
        for field, budget in (("args_summary", 12000), ("output", 32768), ("result", 32768), ("error", 4000), ("transport_error", 2000)):
            raw = original.get(field)
            if field == "output" and isinstance(raw, str):
                # Scrub before slicing, then preserve the most recent output.
                clean = redact_text(raw)
                clipped, scrubbed = len(clean) > budget, clean != raw
                row[field] = ("<… truncated; latest output follows>\n" + clean[-budget:]) if clipped else clean
            else:
                value, clipped, scrubbed = display_value({field: raw}, text_limit=budget, budget=budget)
                row[field] = value[field]
            sections[field] = clipped
            any_clipped |= clipped
            any_scrubbed |= scrubbed
        row["display"] = {"truncated": any_clipped or original.get("output_truncated", False), "redacted": any_scrubbed, "sections": sections}
        row["output_truncated"] = original.get("output_truncated", False) or sections["output"]
        row["arguments_truncated"] |= sections["args_summary"]
        result = row.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and type(data.get("exit_code")) is int:
            row["exit_code"] = data["exit_code"]
        row["timing"] = {"wait_ms": None, "execution_ms": None}
        try:
            # Read evidence directly: polling must not generate more audit calls.
            trace = runtime.diagnostics.trace({"operation_id": identifier, "after_event_id": 0, "limit": 200}, principal)
            row["timing"] = trace_timing(trace, original.get("created"))
            row["trace"], trace_clipped, _ = display_value(trace, text_limit=1000, budget=32768)
            shown = row["trace"]
            if isinstance(shown, dict):
                events = shown.get("events")
                shown["events"] = [e for e in events if isinstance(e, dict) and isinstance(e.get("stage"), str)] if isinstance(events, list) else []
            row["display"]["truncated"] |= trace_clipped
        except DevError as exc:
            row["trace_error"] = redact_text(str(exc))[:2000]
        return row
