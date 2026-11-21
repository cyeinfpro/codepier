"""Administrative, read-only projections of durable tool operations.

Never decrypt the queued request to display a log, and never run an operation
when expanding its entry. Historical records retain their original evidence.
"""

from __future__ import annotations

import asyncio
import base64
import csv
from datetime import datetime
import hashlib
import io
import json
import math
import re
import time
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Query, Request
from fastapi.responses import StreamingResponse

from hub import iam
from hub.db_worker import database_endpoint, run_db, run_db_read
from hub.operation_categories import operation_category_sql
from shared.audit_redaction import display_value, redact_text

TERMINAL = {"succeeded", "failed", "cancelled", "needs_review", "interrupted"}
ACTIVE = {"queued", "running", "reconnecting", "cancelling", "unknown"}
STATES = TERMINAL | ACTIVE
PRESETS = {"", "errors", "active", "slow", "mutations"}
COMMAND_TOOLS = frozenset(
    {"exec", "shell_exec", "ssh_exec", "vps_exec", "tasks_run", "validation_run", "bash"}
)
FILE_TOOLS = frozenset(
    {
        "read",
        "fs_read",
        "fs_read_many",
        "write",
        "fs_write",
        "edit",
        "fs_edit",
        "apply_patch",
        "fs_apply",
        "file",
        "artifact",
        "import",
        "restore",
    }
)
SEARCH_TOOLS = frozenset({"search", "fs_search", "symbols", "collab.search"})
MUTATION_TOOLS = (
    "write",
    "fs_write",
    "edit",
    "fs_edit",
    "apply_patch",
    "fs_apply",
    "exec",
    "shell_exec",
    "ssh_exec",
    "vps_exec",
    "tasks_run",
    "validation_run",
    "restore",
    "import",
)


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def _human_chars(count: int) -> str:
    if count < 1024:
        return f"{count} 字符"
    if count < 1048576:
        return f"{count / 1024:.1f} KB"
    return f"{count / 1048576:.1f} MB"


def _split_path(path: str) -> tuple[str, str]:
    clean = path.replace("\n", " ")[:400]
    if "/" not in clean:
        return "", clean
    prefix, name = clean.rsplit("/", 1)
    return prefix + "/", name


def _contains_ellipsis(value) -> bool:
    if isinstance(value, str):
        return "…" in value
    if isinstance(value, dict):
        return any(_contains_ellipsis(k) or _contains_ellipsis(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_ellipsis(item) for item in value)
    return False


def _scrub_field(value, limit: int = 1600) -> tuple[object, bool, bool]:
    if not isinstance(value, str) or value == "":
        return value, False, False
    clean = redact_text(value)
    scrubbed = clean != value
    if len(clean) > limit:
        return clean[:limit] + "\n<… truncated>", True, scrubbed
    return clean, False, scrubbed


def _effective_args(args: dict) -> tuple[dict, list[dict], list[dict]]:
    options = args.get("options") if isinstance(args.get("options"), dict) else {}
    merged = {
        **options,
        **{k: v for k, v in args.items() if v not in ("", [], None, {})},
    }
    raw_items = merged.get("items") if isinstance(merged.get("items"), list) else []
    items = [
        item
        for item in raw_items
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]
    ]
    raw_changes = merged.get("changes") if isinstance(merged.get("changes"), list) else []
    changes = [
        item
        for item in raw_changes
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]
    ]
    return merged, items, changes


def _batch_paths(args: dict, items: list[dict], changes: list[dict]) -> list[str]:
    paths = args.get("paths")
    if isinstance(paths, list) and paths:
        return [str(p)[:120] for p in paths if str(p)]
    source = items or changes
    return [str(item["path"])[:120] for item in source]


def argument_summary(args: dict) -> str:
    merged, items, changes = _effective_args(args)
    parts = []
    scalar_keys = (
        ("path", "command", "task", "query", "cwd", "timeout_seconds")
        if (items or changes) and not merged.get("path")
        else (
            "path",
            "command",
            "task",
            "query",
            "cwd",
            "start_line",
            "max_lines",
            "offset",
            "timeout_seconds",
        )
    )
    for key in scalar_keys:
        value = merged.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value != "":
            parts.append(f"{key}={str(value).replace(chr(10), ' ↵ ')[:600]}")
    batch = _batch_paths(merged, items, changes)
    if batch and not merged.get("path"):
        parts.append("paths=" + ", ".join(batch[:3]))
        parts.append(f"files={len(batch)}{'+' if len(batch) >= 30 else ''}")
    meta = args.get("_log") if isinstance(args.get("_log"), dict) else {}
    edits = meta.get("edit_count")
    if isinstance(edits, int) and edits >= 0:
        parts.append(f"edits={edits}")
    elif isinstance(merged.get("edits"), list) and merged["edits"]:
        count = len(merged["edits"])
        parts.append(f"edits={count}{'+' if count >= 30 else ''}")
    content = merged.get("content")
    match = re.fullmatch(r"<(\d+) chars>", content) if isinstance(content, str) else None
    if match:
        parts.append("inputChars=" + match[1])
    chars = meta.get("command_chars")
    if isinstance(chars, int) and chars >= 0:
        parts.append(f"commandChars={chars}")
    if not parts:
        op = merged.get("operation")
        if isinstance(op, str) and op not in {"", "file", "batch"}:
            parts.append(f"operation={op[:80]}")
    return "  ".join(parts)[:1200]


def _count_badges(args: dict) -> list[str]:
    merged, _, changes = _effective_args(args)
    badges = []
    meta = args.get("_log") if isinstance(args.get("_log"), dict) else {}
    edits = meta.get("edit_count")
    if isinstance(edits, int) and edits > 0:
        badges.append(f"{edits} 处修改")
    elif isinstance(merged.get("edits"), list) and merged["edits"]:
        count = len(merged["edits"])
        badges.append(f"{count}{'+' if count >= 30 else ''} 处修改")
    elif changes:
        nested_edits = sum(
            len(item["edits"]) for item in changes if isinstance(item.get("edits"), list)
        )
        if nested_edits > 0:
            badges.append(f"{nested_edits} 处修改")
    if merged.get("dry_run") is True:
        badges.append("演练")
    content = merged.get("content")
    match = re.fullmatch(r"<(\d+) chars>", content) if isinstance(content, str) else None
    if match:
        badges.append(_human_chars(int(match[1])))
    chars = meta.get("command_chars")
    if isinstance(chars, int) and chars > 0:
        badges.append(_human_chars(chars))
    return badges


def _command_summary_parts(args: dict, badges: list[str]) -> dict | None:
    raw_cmd = args.get("command") or args.get("task") or args.get("label") or ""
    primary = str(raw_cmd).replace("\n", " ↵ ")[:600] if raw_cmd != "" else ""
    cwd = args.get("cwd")
    if isinstance(cwd, str) and cwd not in {"", "."}:
        badges = [f"cwd: {cwd[:80]}", *badges]
    timeout = args.get("timeout_seconds")
    if (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and timeout not in {3600, 30}
    ):
        badges = [*badges, f"限时 {int(timeout)}s"]
    if not primary and not badges:
        return None
    return {"kind": "command", "primary": primary, "dir": "", "name": "", "badges": badges}


def _line_range_badge(source: dict) -> str:
    start = source.get("start_line") or source.get("offset")
    limit = source.get("max_lines") or source.get("limit")
    if isinstance(start, int) and not isinstance(start, bool) and start > 0:
        if start == 1 and limit in (None, 2000):
            return ""
        return f"L{start}" + (f"+{limit}" if isinstance(limit, int) and limit > 0 else "")
    return ""


def _file_summary_parts(
    merged: dict, items: list[dict], changes: list[dict], badges: list[str]
) -> dict | None:
    path_val = merged.get("path")
    batch = _batch_paths(merged, items, changes)
    line_source = merged
    if isinstance(path_val, str) and path_val not in {"", "."}:
        directory, filename = _split_path(path_val)
        primary = directory + filename
    elif len(batch) == 1:
        directory, filename = _split_path(batch[0])
        primary = directory + filename
        if len(items) == 1:
            line_source = items[0]
    elif batch:
        primary = ", ".join(batch[:3])
        directory, filename = "", primary
        badges = [f"{len(batch)}{'+' if len(batch) >= 30 else ''} 个文件", *badges]
    elif path_val == ".":
        primary, directory, filename = ".", "", "."
    else:
        primary = directory = filename = ""
    line_tag = _line_range_badge(line_source)
    if line_tag:
        badges = [line_tag, *badges]
    if not primary and not badges:
        return None
    return {
        "kind": "file",
        "primary": primary,
        "dir": directory,
        "name": filename,
        "badges": badges,
    }


def structured_argument_summary(tool: str, args: dict) -> dict:
    merged, items, changes = _effective_args(args)
    badges = _count_badges(args)
    if tool in COMMAND_TOOLS or "command" in merged or "task" in merged:
        cmd_parts = _command_summary_parts(merged, badges)
        if cmd_parts:
            return cmd_parts
    is_search = (
        tool in SEARCH_TOOLS
        or merged.get("operation") in {"search", "search_start"}
        or ("query" in merged and not merged.get("path"))
    )
    if is_search and merged.get("query"):
        query_val = str(merged.get("query") or "").replace("\n", " ")[:300]
        path_val = merged.get("path")
        search_badges = (
            [f"范围 {path_val[:80]}", *badges]
            if isinstance(path_val, str) and path_val not in {"", "."}
            else badges
        )
        return {
            "kind": "search",
            "primary": query_val,
            "dir": "",
            "name": "",
            "badges": search_badges,
        }
    if (
        tool in FILE_TOOLS
        or merged.get("path")
        or items
        or changes
        or isinstance(merged.get("paths"), list)
    ):
        file_parts = _file_summary_parts(merged, items, changes, badges)
        if file_parts:
            return file_parts
    fallback = argument_summary(args)
    if not fallback:
        return {"kind": "empty", "primary": "", "dir": "", "name": "", "badges": []}
    return {"kind": "generic", "primary": fallback, "dir": "", "name": "", "badges": badges}


def operation_summary(args: dict, *, maintenance=False) -> dict:
    """Persist counts alongside the existing privacy-preserving summary."""
    from shared.util import safe_summary

    result = safe_summary(args)
    # Only trusted admission context supplies origin; never copy request _log.
    meta = {"version": 2, "trigger_source": "system_maintenance" if maintenance else "request"}
    if maintenance:
        meta["trigger_reason"] = "automatic_refresh"
    if isinstance(args.get("command"), str):
        meta["command_chars"] = len(args["command"])
    if isinstance(args.get("edits"), list):
        meta["edit_count"] = len(args["edits"])
    result["_log"] = meta
    return result


def public_row(row: dict, now: float | None = None) -> dict:
    passthrough = (
        "id",
        "project_id",
        "device_id",
        "tool",
        "state",
        "created",
        "updated",
        "attempts",
        "accepted_at",
        "deadline",
        "cancel_requested",
    )
    result = {key: row[key] for key in passthrough if key in row}
    args, clipped, scrubbed = display_value(
        _object(row.get("args_summary")), text_limit=1200, budget=12000
    )
    more_clipped = more_scrubbed = False
    for text_key in ("actor", "error", "transport_error", "alias", "device_name"):
        if text_key in row:
            cleaned, field_clipped, field_scrubbed = _scrub_field(row[text_key], limit=1600)
            result[text_key] = cleaned
            more_clipped |= field_clipped
            more_scrubbed |= field_scrubbed
    tool_name = str(row.get("tool") or "")
    result["summary"] = argument_summary(args)
    result["summary_parts"] = structured_argument_summary(tool_name, args)
    meta = args.get("_log") if isinstance(args.get("_log"), dict) else {}
    source = meta.get("trigger_source") if meta.get("version") == 2 else None
    result["trigger_source"] = source if source in {"system_maintenance", "request"} else "unknown"
    result["trigger_reason"] = (
        "automatic_refresh"
        if source == "system_maintenance" and meta.get("trigger_reason") == "automatic_refresh"
        else None
    )
    result["model_invocation"] = False if tool_name == "project_knowledge_build" else None
    result["knowledge_mode"] = None
    result["arguments_truncated"] = clipped or _contains_ellipsis(args)
    created = result.get("created")
    end = (
        result.get("updated")
        if result.get("state") in TERMINAL
        else (time.time() if now is None else now)
    )
    valid_times = all(
        type(value) in (float, int) and math.isfinite(value) for value in (created, end)
    )
    result["elapsed_ms"] = max(0, round((end - created) * 1000)) if valid_times else None
    result["exit_code"] = None
    result["display"] = {
        "truncated": clipped or more_clipped,
        "redacted": scrubbed or more_scrubbed,
    }
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
            if (
                wait is None
                and isinstance(observed, (int, float))
                and isinstance(created, (int, float))
                and observed >= created
            ):
                wait = round((observed - created) * 1000)
            if accepted is not None:
                start = event
        elif stage == "persisting" and start is not None:
            begin = start.get("elapsed_ms")
            first_seq = start.get("seq")
            if (
                type(elapsed) is int
                and type(begin) is int
                and elapsed >= begin
                and type(seq) is int
                and type(first_seq) is int
                and seq > first_seq
            ):
                execution = elapsed - begin
            start = None
    return {"wait_ms": wait, "execution_ms": execution}


def _filter_id(filters):
    return hashlib.sha256(
        json.dumps(filters, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:20]


def _cursor(created, identifier, filters):
    return (
        base64.urlsafe_b64encode(
            json.dumps(
                [1, created, identifier, _filter_id(filters)], separators=(",", ":")
            ).encode()
        )
        .decode()
        .rstrip("=")
    )


def _decode_cursor(cursor, filters):
    from shared.util import DevError

    try:
        raw = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        version, created, identifier, fingerprint = raw
        if (
            version != 1
            or isinstance(created, bool)
            or not isinstance(created, (int, float))
            or not math.isfinite(created)
            or created < 0
        ):
            raise ValueError()
        if (
            not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 100
            or fingerprint != _filter_id(filters)
        ):
            raise ValueError()
        return created, identifier
    except (ValueError, TypeError, OverflowError, UnicodeError) as exc:
        raise DevError("INVALID_CURSOR", "调用列表游标无效或筛选已变化，请回到第一页") from exc


def _apply_preset_where(where: list[str], values: list, preset: str, slow_ms: int):
    if preset == "errors":
        where.append("o.state IN ('failed','interrupted','needs_review')")
    elif preset == "active":
        where.append("o.state IN ('queued','running','reconnecting','cancelling','unknown')")
    elif preset == "mutations":
        marks = ",".join("?" for _ in MUTATION_TOOLS)
        where.append(f"o.tool IN ({marks})")
        values.extend(MUTATION_TOOLS)
    if slow_ms > 0:
        where.append(
            "(CASE WHEN o.state IN ('succeeded','failed','cancelled','needs_review','interrupted') "
            "THEN (o.updated - o.created) * 1000.0 "
            "ELSE (strftime('%s','now') - o.created) * 1000.0 END) >= ?"
        )
        values.append(float(slow_ms))


def _validate_list_filters(
    principal,
    *,
    q: str,
    source: str,
    trigger_source: str,
    status: str,
    preset: str,
    min_elapsed_ms: int,
    project: str,
    tool: str,
    category: str,
) -> tuple[dict, int]:
    from shared.util import DevError

    if (
        trigger_source not in {"", "system_maintenance", "request", "unknown"}
        or source not in {"", "mcp", "panel"}
        or status not in STATES | {""}
        or preset not in PRESETS
    ):
        raise DevError("INVALID_FILTER", "无效的调用来源或状态")
    slow_ms = max(min_elapsed_ms, 5000 if preset == "slow" else 0)
    filters = {
        "q": q.strip(),
        "source": source,
        "trigger_source": trigger_source,
        "status": status,
        "project": project,
        "tool": tool,
        "space": principal.space_id,
        "user": principal.user_id,
        "epoch": principal.user_epoch,
    }
    if category != "all":
        filters["category"] = category
    if preset:
        filters["preset"] = preset
    if slow_ms > 0:
        filters["min_elapsed_ms"] = slow_ms
    return filters, slow_ms


def _prepare_list_query(
    principal,
    *,
    q: str,
    source: str,
    trigger_source: str,
    status: str,
    preset: str,
    min_elapsed_ms: int,
    project: str,
    tool: str,
    category: str,
):
    filters, slow_ms = _validate_list_filters(
        principal,
        q=q,
        source=source,
        trigger_source=trigger_source,
        status=status,
        preset=preset,
        min_elapsed_ms=min_elapsed_ms,
        project=project,
        tool=tool,
        category=category,
    )
    clause, scope_args = iam.private_sql(principal, "o.")
    if not principal.admin:
        clause += " AND (o.project_id IS NULL OR o.project_id IN (%s))" % (
            ",".join("?" for _ in principal.projects) or "NULL"
        )
        scope_args.extend(principal.projects)
    category_clause, category_args = operation_category_sql(category, "o.")
    clause += " AND " + category_clause
    scope_args.extend(category_args)
    where, values = [clause], list(scope_args)
    for column, value in (("o.state", status), ("o.project_id", project), ("o.tool", tool)):
        if value:
            where.append(column + "=?")
            values.append(value)
    _apply_preset_where(where, values, preset, slow_ms)
    if source:
        where.append("o.actor LIKE ?")
        values.append(source + ":%")
    if trigger_source:
        origin = """CASE WHEN json_valid(o.args_summary)
            THEN CASE WHEN json_extract(o.args_summary,'$._log.version')=2
                 AND json_extract(o.args_summary,'$._log.trigger_source') IN ('system_maintenance','request')
                 THEN json_extract(o.args_summary,'$._log.trigger_source')
                 ELSE 'unknown' END ELSE 'unknown' END"""
        where.append("(" + origin + ")=?")
        values.append(trigger_source)
    if filters["q"]:
        literal = filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        columns = ("o.id", "o.actor", "o.tool", "o.args_summary", "o.error", "p.alias", "d.name")
        where.append("(" + " OR ".join(column + " LIKE ? ESCAPE '\\'" for column in columns) + ")")
        values.extend(["%" + literal + "%"] * len(columns))
    return filters, clause, scope_args, where, values


def _attach_active_contention(
    runtime, principal, raw_rows: list[dict], projected: list[dict], now: float
):
    if not projected or not hasattr(runtime, "batch_queue_annotations"):
        return
    active_raw = [row for row in raw_rows if row.get("state") in ACTIVE]
    if not active_raw:
        return
    annotations = runtime.batch_queue_annotations(active_raw, principal, now=now)
    for item in projected:
        note = annotations.get(item.get("id"))
        if note and item.get("state") in ACTIVE:
            item["queue_stage"] = note.get("queue_stage")
            if "waiting" in note:
                item["waiting"] = note["waiting"]


def _format_export_payload(
    rows: list[dict], fmt: str, *, include_header: bool = True
) -> tuple[str, str, str]:
    if fmt == "ndjson":
        body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        if body:
            body += "\n"
        return body, "application/x-ndjson; charset=utf-8", "codepier-call-log.ndjson"
    out = io.StringIO()
    writer = csv.writer(out)
    if include_header:
        writer.writerow(
            [
                "id",
                "utc_time",
                "project",
                "device",
                "tool",
                "state",
                "elapsed_ms",
                "actor",
                "trigger_source",
                "summary",
                "error",
            ]
        )

    def cell(value):
        text = "" if value is None else str(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text

    for item in rows:
        created = item.get("created")
        utc_str = (
            datetime.fromtimestamp(created, ZoneInfo("UTC")).isoformat()
            if isinstance(created, (int, float)) and math.isfinite(created)
            else ""
        )
        writer.writerow(
            [
                item.get("id", ""),
                utc_str,
                cell(item.get("alias") or ""),
                cell(item.get("device_name") or ""),
                cell(item.get("tool") or ""),
                cell(item.get("state") or ""),
                item.get("elapsed_ms") if item.get("elapsed_ms") is not None else "",
                cell(item.get("actor") or ""),
                cell(item.get("trigger_source") or ""),
                cell(item.get("summary") or ""),
                cell(item.get("error") or ""),
            ]
        )
    return (
        ("\ufeff" if include_header else "") + out.getvalue(),
        "text/csv; charset=utf-8",
        "codepier-call-log.csv",
    )


def _output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _detail_sections(
    original: dict, row: dict, after_output_chars: int | None, after_output_sha: str = ""
):
    sections = {}
    any_clipped = any_scrubbed = False
    for field, budget in (
        ("args_summary", 12000),
        ("output", 32768),
        ("result", 32768),
        ("error", 4000),
        ("transport_error", 2000),
    ):
        if field == "output" and original.get("output_unchanged"):
            row["output"] = None
            row["output_unchanged"] = True
            sections["output"] = False
            continue
        raw = original.get(field)
        if field == "output" and isinstance(raw, str):
            # Scrub before slicing, then preserve the most recent output.
            clean = redact_text(raw)
            clipped, scrubbed = len(clean) > budget, clean != raw
            row[field] = (
                ("<… truncated; latest output follows>\n" + clean[-budget:]) if clipped else clean
            )
            row["output_clean_chars"] = len(clean)
            if not clipped:
                row["output_clean_sha"] = _output_digest(clean)
            # Redaction is not prefix-stable: a secret that completes in a later
            # chunk can rewrite text the client already holds. Send a suffix only
            # when the client's copy is byte-identical to the current redacted
            # prefix; otherwise the full (redacted) output replaces it.
            if (
                not clipped
                and isinstance(after_output_chars, int)
                and 0 < after_output_chars <= len(clean)
                and after_output_sha
                and _output_digest(clean[:after_output_chars]) == after_output_sha
            ):
                row["output_delta"] = clean[after_output_chars:]
                row["output_base_chars"] = after_output_chars
        else:
            value, clipped, scrubbed = display_value({field: raw}, text_limit=budget, budget=budget)
            row[field] = value[field]
        sections[field] = clipped
        any_clipped |= clipped
        any_scrubbed |= scrubbed
    row["display"] = {
        "truncated": any_clipped or original.get("output_truncated", False),
        "redacted": any_scrubbed,
        "sections": sections,
    }
    row["output_seq"] = original.get("output_seq", 0)
    row["output_truncated"] = original.get("output_truncated", False) or sections["output"]
    row["arguments_truncated"] |= sections["args_summary"]


def _enrich_detail_metadata(original: dict, row: dict):
    result = row.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict) and type(data.get("exit_code")) is int:
        row["exit_code"] = data["exit_code"]
    if row.get("tool") == "project_knowledge_build" and isinstance(data, dict):
        refresh = data.get("refresh") if isinstance(data.get("refresh"), dict) else {}
        mode = refresh.get("mode")
        row["knowledge_mode"] = mode if mode in {"full", "incremental"} else None
        row["knowledge_stats"] = {
            key: data[key]
            for key in ("entries_count", "changed_entries", "reused_entries", "skipped_files")
            if type(data.get(key)) is int and data[key] >= 0
        }
        if type(refresh.get("metadata_reused")) is int and refresh["metadata_reused"] >= 0:
            row["knowledge_stats"]["metadata_reused"] = refresh["metadata_reused"]
    row.update({key: original[key] for key in ("queue_stage", "waiting") if key in original})
    if isinstance(data, dict) and isinstance(data.get("concurrency"), dict):
        row["concurrency"] = data["concurrency"]


def _parse_watch(watch: str) -> list[str]:
    from shared.util import DevError

    watched = list(dict.fromkeys(watch.split(","))) if watch else []
    if len(watched) > 40 or any(not re.fullmatch(r"[a-f0-9]{32}", x) for x in watched):
        raise DevError("INVALID_FILTER", "无效的调用状态订阅")
    return watched


EXPORT_BATCH_SIZE = 200


def _render_export(raw_rows: list[dict], observed: float, fmt: str, *, include_header=True):
    sanitized = [public_row(row, observed) for row in raw_rows]
    return (
        len(sanitized),
        *_format_export_payload(sanitized, fmt, include_header=include_header),
    )


def _audit_export(store, principal, *, rows: int, fmt: str, truncated: bool):
    # auth.panel() bound the caller's Space inside the read phase's copied
    # Context. Rebind it here, otherwise Store.audit falls back to the legacy
    # Space and misfiles a tenant's export record.
    token = iam.audit_context.set((store, principal.actor, principal.space_id, principal.user_id))
    try:
        store.audit(
            principal.actor,
            "call_log.exported",
            detail={"rows": rows, "format": fmt, "limit": 10000, "truncated": truncated},
        )
    finally:
        iam.audit_context.reset(token)


def _read_export_batch(store, auth, request, snapshot_filters, select_base, batch, **options):
    from shared.util import DevError

    with iam.read_scope(store):
        current = auth.panel(request)
        filters, clause, scope_args, _, _ = _prepare_list_query(current, **options)
        if filters != snapshot_filters:
            raise DevError("AUTHORIZATION_CHANGED", "导出期间身份发生变化，请重新导出", 403)
        ids = [item["id"] for item in batch]
        marks = ",".join("?" for _ in ids)
        # Repeat current tenant/owner/project authorization on each chunk, but
        # retain originally selected filter membership.
        rows = store.all(
            select_base()
            + " WHERE "
            + clause
            + f" AND o.id IN ({marks}) ORDER BY o.created DESC,o.id DESC",
            (*scope_args, *ids),
        )
        if len(rows) != len(ids):
            # Never silently deliver an apparently complete export after
            # access is narrowed or a selected record disappears.
            raise DevError("AUTHORIZATION_CHANGED", "导出范围已变化，请重新导出", 403)
        return rows


def _register_call_log_export(app, runtime, auth, select_base):
    store = runtime.store

    @app.get("/api/call-log-export")
    async def call_log_export(
        request: Request,
        cursor: str = Query("", max_length=1024),
        q: str = Query("", max_length=200),
        source: str = Query("", max_length=20),
        trigger_source: str = Query("", max_length=30),
        status: str = Query("", max_length=30),
        preset: str = Query("", max_length=30),
        min_elapsed_ms: int = Query(0, ge=0, le=86400000),
        project: str = Query("", max_length=100),
        tool: str = Query("", max_length=100),
        format: Literal["csv", "ndjson"] = "csv",
        category: Literal["all", "tools", "project_map"] = "tools",
    ):
        def read_snapshot():
            with iam.read_scope(store):
                principal = auth.panel(request)
                filters, _, _, where, values = _prepare_list_query(
                    principal,
                    q=q,
                    source=source,
                    trigger_source=trigger_source,
                    status=status,
                    preset=preset,
                    min_elapsed_ms=min_elapsed_ms,
                    project=project,
                    tool=tool,
                    category=category,
                )
                if cursor:
                    created, identifier = _decode_cursor(cursor, filters)
                    where.append("(o.created < ? OR (o.created = ? AND o.id < ?))")
                    values.extend([created, created, identifier])
                # Freeze only the bounded ID set, not 10k wide summaries. New
                # arrivals cannot slip into later chunks (even with tied times).
                joins = (
                    " LEFT JOIN projects p ON p.id=o.project_id"
                    " LEFT JOIN devices d ON d.id=o.device_id"
                    if filters["q"]
                    else ""
                )
                keys = store.all(
                    "SELECT o.id,o.created FROM operations o"
                    + joins
                    + " WHERE "
                    + " AND ".join(where)
                    + " ORDER BY o.created DESC,o.id DESC LIMIT ?",
                    (*values, 10001),
                )
                truncated = len(keys) > 10000
                keys = keys[:10000]
                next_cursor = (
                    _cursor(keys[-1]["created"], keys[-1]["id"], filters)
                    if truncated and keys
                    else ""
                )
                return principal, keys, filters, truncated, next_cursor, time.time()

        principal, keys, snapshot_filters, truncated, next_cursor, observed = await run_db_read(
            store, read_snapshot
        )

        def read_batch(batch):
            return _read_export_batch(
                store,
                auth,
                request,
                snapshot_filters,
                select_base,
                batch,
                q=q,
                source=source,
                trigger_source=trigger_source,
                status=status,
                preset=preset,
                min_elapsed_ms=min_elapsed_ms,
                project=project,
                tool=tool,
                category=category,
            )

        first_rows = await run_db_read(store, read_batch, keys[:EXPORT_BATCH_SIZE]) if keys else []
        _, first_body, media_type, filename = await asyncio.to_thread(
            _render_export, first_rows, observed, format
        )
        if hasattr(store, "audit"):
            await run_db(
                store,
                _audit_export,
                store,
                principal,
                rows=len(keys),
                fmt=format,
                truncated=truncated,
            )

        async def stream():
            # No database transaction or writer lock is held across yield.
            # Memory for summaries and serialized output is bounded to 200 rows;
            # the snapshot contains at most 10k small (id, created) pairs.
            yield first_body
            for offset in range(EXPORT_BATCH_SIZE, len(keys), EXPORT_BATCH_SIZE):
                if await request.is_disconnected():
                    return
                rows = await run_db_read(
                    store, read_batch, keys[offset : offset + EXPORT_BATCH_SIZE]
                )
                _, body, _, _ = await asyncio.to_thread(
                    _render_export, rows, observed, format, include_header=False
                )
                yield body

        return StreamingResponse(
            stream(),
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Export-Limit": "10000",
                "X-Export-Truncated": str(truncated).lower(),
                "X-Export-Rows": str(len(keys)),
                "X-Export-Next-Cursor": next_cursor,
                "X-Export-Snapshot": "selected-identifiers",
            },
        )


def register_call_log(app, runtime, auth):
    from shared.util import DevError

    store = runtime.store
    select_cols = (
        "SELECT o.id,o.project_id,o.device_id,o.tool,o.actor,o.state,o.created,o.updated,"
        "o.args_summary,o.error,o.attempts,o.accepted_at,p.alias,d.name AS device_name "
        "FROM operations o LEFT JOIN projects p ON p.id=o.project_id "
        "LEFT JOIN devices d ON d.id=o.device_id"
    )
    select_compat_cols = (
        "SELECT o.id,o.project_id,o.device_id,o.tool,o.actor,o.state,o.created,o.updated,"
        "o.args_summary,o.error,o.attempts,p.alias,d.name AS device_name "
        "FROM operations o LEFT JOIN projects p ON p.id=o.project_id "
        "LEFT JOIN devices d ON d.id=o.device_id"
    )

    def _select_base() -> str:
        return select_cols if hasattr(runtime, "batch_queue_annotations") else select_compat_cols

    @app.get("/api/call-log")
    @database_endpoint(store, read_only=True)
    def call_log(
        request: Request,
        limit: int = Query(40, ge=1, le=100),
        cursor: str = Query("", max_length=1024),
        q: str = Query("", max_length=200),
        source: str = Query("", max_length=20),
        trigger_source: str = Query("", max_length=30),
        status: str = Query("", max_length=30),
        preset: str = Query("", max_length=30),
        min_elapsed_ms: int = Query(0, ge=0, le=86400000),
        project: str = Query("", max_length=100),
        tool: str = Query("", max_length=100),
        watch: str = Query("", max_length=4096),
        include_filters: bool = True,
        include_total: bool | None = None,
        include_queue: bool = False,
        category: Literal["all", "tools", "project_map"] = "all",
    ):
        with iam.read_scope(store):
            principal = auth.panel(request)
            filters, clause, scope_args, where, values = _prepare_list_query(
                principal,
                q=q,
                source=source,
                trigger_source=trigger_source,
                status=status,
                preset=preset,
                min_elapsed_ms=min_elapsed_ms,
                project=project,
                tool=tool,
                category=category,
            )
            joins = (
                " LEFT JOIN projects p ON p.id=o.project_id LEFT JOIN devices d ON d.id=o.device_id"
                if filters["q"]
                else ""
            )
            watched = _parse_watch(watch)
            should_count = include_filters if include_total is None else include_total
            total = None
            total_capped = False
            if should_count:
                count_sql = (
                    "SELECT count(*) AS n FROM (SELECT 1 FROM operations o"
                    + joins
                    + " WHERE "
                    + " AND ".join(where)
                    + " LIMIT 1001)"
                )
                raw_total = store.all(count_sql, values)[0]["n"]
                total_capped = raw_total > 1000
                total = min(raw_total, 1000)
            if cursor:
                created, identifier = _decode_cursor(cursor, filters)
                where.append("(o.created < ? OR (o.created = ? AND o.id < ?))")
                values.extend([created, created, identifier])
            base_sql = _select_base()
            sql = base_sql + (" WHERE " + " AND ".join(where) if where else "")
            rows = store.all(
                sql + " ORDER BY o.created DESC,o.id DESC LIMIT ?", (*values, limit + 1)
            )
            more = len(rows) > limit
            rows = rows[:limit]
            observed = time.time()
            # Rows already on this page carry their current state. The client
            # watches the page it shows, so re-reading and re-redacting the same
            # IDs as "updates" doubled every head poll; fetch only the rest.
            listed = {row["id"] for row in rows}
            pending_watch = [x for x in watched if x not in listed]
            updates = []
            if pending_watch:
                updates = store.all(
                    base_sql
                    + " WHERE "
                    + clause
                    + " AND o.id IN ("
                    + ",".join("?" for _ in pending_watch)
                    + ")",
                    (*scope_args, *pending_watch),
                )
            projected_rows = [public_row(row, observed) for row in rows]
            projected_updates = [public_row(row, observed) for row in updates]
            _attach_active_contention(
                runtime,
                principal,
                [*rows, *updates],
                [*projected_rows, *projected_updates],
                observed,
            )
            payload = {
                "category": category,
                "total": total,
                "total_capped": total_capped,
                "updates": projected_updates,
                "operations": projected_rows,
                "next_cursor": _cursor(rows[-1]["created"], rows[-1]["id"], filters)
                if more
                else None,
                "observed_at": observed,
                "projects": [
                    {k: row[k] for k in ("id", "alias")} for row in runtime.list_projects(principal)
                ]
                if include_filters
                else [],
                "tools": [
                    x["tool"]
                    for x in store.all(
                        "SELECT DISTINCT o.tool FROM operations o WHERE "
                        + clause
                        + " ORDER BY o.tool LIMIT 200",
                        scope_args,
                    )
                ]
                if include_filters
                else [],
            }
            if include_queue and hasattr(runtime, "authorize"):
                from hub.queue_panel import queue_rows

                payload["queue"] = queue_rows(store, runtime, principal)
            return payload

    _register_call_log_export(app, runtime, auth, _select_base)

    @app.get("/api/call-log/{identifier}")
    @database_endpoint(store, read_only=True)
    def call_log_detail(
        identifier: str,
        request: Request,
        after_output_seq: int | None = Query(None, ge=0),
        after_output_chars: int | None = Query(None, ge=0),
        after_output_sha: str = Query("", max_length=64, pattern=r"^[a-f0-9]*$"),
        after_event_id: int = Query(0, ge=0),
    ):
        with iam.read_scope(store):
            principal = auth.panel(request)
            original = (
                runtime.operation(identifier, principal, {"after_output_seq": after_output_seq})
                if after_output_seq is not None
                else runtime.operation(identifier, principal)
            )
            row = public_row(
                {key: value for key, value in original.items() if key not in {"result", "output"}}
            )
            _detail_sections(original, row, after_output_chars, after_output_sha)
            _enrich_detail_metadata(original, row)
            row["timing"] = {"wait_ms": None, "execution_ms": None}
            try:
                # Read evidence directly: polling must not generate more audit calls.
                trace = runtime.diagnostics.trace(
                    {"operation_id": identifier, "after_event_id": after_event_id, "limit": 200},
                    principal,
                )
                if after_event_id == 0:
                    row["timing"] = trace_timing(trace, original.get("created"))
                else:
                    row["trace_incremental"] = True
                row["trace"], trace_clipped, _ = display_value(trace, text_limit=1000, budget=32768)
                shown = row["trace"]
                if isinstance(shown, dict):
                    events = shown.get("events")
                    shown["events"] = (
                        [
                            e
                            for e in events
                            if isinstance(e, dict) and isinstance(e.get("stage"), str)
                        ]
                        if isinstance(events, list)
                        else []
                    )
                    raw_events = trace.get("events") if isinstance(trace, dict) else None
                    if isinstance(raw_events, list) and raw_events:
                        last_id = raw_events[-1].get("id")
                        if type(last_id) is int:
                            shown["latest_event_id"] = last_id
                row["display"]["truncated"] |= trace_clipped
            except DevError as exc:
                row["trace_error"] = redact_text(str(exc))[:2000]
            return row
