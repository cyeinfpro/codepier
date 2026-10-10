"""Current-authority MCP text summaries plus bounded context scenarios."""
from __future__ import annotations

from collections import defaultdict, OrderedDict
from copy import deepcopy
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Literal

from hub import iam
from hub.context_usage import context_usage
from hub.mcp_usage import MAX_ROWS, RETENTION_SECONDS, activity_actor_scope
from hub.principal import refresh_principal
from shared.context_estimate import VERSION as CONTEXT_VERSION
from shared.token_estimate import summarize, NUMBERS, VERSION as TEXT_VERSION, unavailable
from shared.token_cost import DEFAULT_CACHE_READ_PERCENT, DEFAULT_REFERENCE_MODEL, MODELS, VERSION as PRICE_VERSION, pricing
from shared.util import DevError

Period = Literal["today", "7d", "30d"]
CACHE_SECONDS = 30
CACHE_ENTRIES = 8


def _safe_usage(value):
    if not isinstance(value, dict) or value.get("version") != TEXT_VERSION or value.get("kind") != "estimate" or value.get("scope") != "project_tool_payload":
        raise ValueError("Invalid usage metadata")
    result = {"version": TEXT_VERSION, "kind": "estimate", "scope": "project_tool_payload", "actual_usage": None}
    for direction in ("input", "output"):
        side = value.get(direction) or unavailable()
        if side.get("state") == "unavailable":
            result[direction] = unavailable()
            continue
        if side.get("state") not in {"available", "partial"} or any(type(side.get(key)) is not int or side[key] < 0 for key in NUMBERS):
            raise ValueError("Invalid usage counters")
        if not side["low"] <= side["estimated_tokens"] <= side["high"]:
            raise ValueError("Invalid usage interval")
        result[direction] = {key: side[key] for key in NUMBERS}
        result[direction].update(state=side["state"], source_truncated=bool(side.get("source_truncated")),
                                 truncated=bool(side.get("truncated")), excluded_fields=side.get("excluded_fields", 0))
    return result


def token_dashboard(runtime, principal, timezone, *, period: Period = "today",
                    project: str = "", connection: str = "", session: str = "",
                    cache_read_percent: int = DEFAULT_CACHE_READ_PERCENT,
                    reference_model: str = DEFAULT_REFERENCE_MODEL,
                    now: float | None = None):
    """Revalidate authority even on cache hits; never cache identity decisions.

    SQLite revisions cover late metric inserts, direct updates, remaps and
    external-connection writes. Cached values contain aggregates only, are
    copied on return, and expire before a retention/day boundary.
    """
    if period not in {"today", "7d", "30d"}:
        raise DevError("INVALID_PERIOD", "不支持的统计时间范围", 400)
    if not isinstance(reference_model, str) or reference_model not in MODELS:
        raise DevError("INVALID_REFERENCE_MODEL", "不支持的参考计价模型", 400)
    try:
        pricing(cache_read_percent, reference_model)
    except ValueError as error:
        raise DevError("INVALID_CACHE_ASSUMPTION", "缓存读取假设须为 0–100 的整数百分比", 400) from error
    store = runtime.store
    with store.lock, iam.read_scope(store):
        principal = refresh_principal(store, principal)
        projects = runtime.list_projects(principal)
        if project and project not in {item["id"] for item in projects}:
            raise DevError("PROJECT_NOT_FOUND", "未找到已授权项目", 404)
        finish = datetime.fromtimestamp(now, timezone) if now is not None else datetime.now(timezone)
        midnight = finish.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight - timedelta(days={"today": 0, "7d": 6, "30d": 29}[period])
        lower, upper = start.timestamp(), finish.timestamp()
        actor_clause, actor_args = activity_actor_scope(store, principal, "a.")
        authority_key = hashlib.sha256(json.dumps([
            principal.user_id, principal.space_id, principal.grant_id, principal.actor,
            principal.admin, principal.instance_admin, sorted(principal.scopes),
            principal.user_epoch, principal.profile_id, principal.role_id, principal.authorization_mode,
            sorted((item["id"], item["root"], item["device_id"], item["alias"]) for item in projects),
        ], separators=(",", ":")).encode()).hexdigest()
        data_version = store.one("PRAGMA data_version")["data_version"]
        collector = runtime.integrations.usage_metrics
        cache_key = (CONTEXT_VERSION, PRICE_VERSION, authority_key, store.session_revision,
                     store.db.total_changes, data_version, id(collector), str(timezone),
                     period, project, connection, session, cache_read_percent, reference_model,
                     lower, now)
        cache = getattr(runtime, "_token_dashboard_cache", None)
        if cache is None:
            cache = runtime._token_dashboard_cache = OrderedDict()
        cached = cache.get(cache_key)
        monotonic = time.monotonic()
        if cached and cached[0] > monotonic and cached[2] > time.time():
            cache.move_to_end(cache_key)
            return deepcopy(cached[1])
        # Only current authorized mappings enter the retained warm-up query.
        visible_projects = [item for item in projects if not project or item["id"] == project]
        rows = []
        collection_unavailable = collector is None
        retention_now = time.time()
        usage_floor = retention_now - RETENTION_SECONDS
        history_floor = upper - RETENTION_SECONDS
        for offset in range(0, len(visible_projects), 100):
            batch = visible_projects[offset:offset + 100]
            mappings = " OR ".join("(a.project=? AND a.root=? AND a.device=?)" for _ in batch)
            values = [value for item in batch for value in (item["id"], item["root"], item["device_id"])]
            where = f"({mappings}) AND a.started>=? AND a.started<=? AND ({actor_clause})"
            where += " AND (a.grant_id IS NULL OR EXISTS (SELECT 1 FROM grants sg WHERE sg.id=a.grant_id AND sg.space_id=?))"
            values.extend((history_floor, upper, *actor_args, principal.space_id))
            columns = ("a.id,a.project,a.root,a.device,a.grant_id,a.actor,a.window_key,a.started,"
                       "a.tool,a.service_ms,a.operation_id,g.label AS connection_label")
            source = " FROM mcp_activity a LEFT JOIN grants g ON g.id=a.grant_id "
            if collector is not None:
                try:
                    items = store.all("SELECT " + columns + ",u.created AS usage_created,u.metrics AS usage_json" +
                                      source + "LEFT JOIN mcp_usage u ON u.activity_id=a.id AND u.created>=? WHERE " +
                                      where + " ORDER BY a.id DESC LIMIT ?", (usage_floor, *values, MAX_ROWS))
                except sqlite3.Error:
                    collection_unavailable = True
                    items = store.all("SELECT " + columns + source + "WHERE " + where +
                                      " ORDER BY a.id DESC LIMIT ?", (*values, MAX_ROWS))
            else:
                items = store.all("SELECT " + columns + source + "WHERE " + where +
                                  " ORDER BY a.id DESC LIMIT ?", (*values, MAX_ROWS))
            rows.extend(items)
            rows = sorted(rows, key=lambda row: row["id"], reverse=True)[:MAX_ROWS]
        rows = sorted({row["id"]: row for row in rows}.values(), key=lambda row: row["id"], reverse=True)[:MAX_ROWS]
        # Options preserve their original selected-period meaning; prior rows
        # are warm-up only and never expose additional labels or window IDs.
        period_rows = [row for row in rows if row["started"] >= lower]
        connections = {row["grant_id"] or "panel": row.get("connection_label") or "管理面板" for row in period_rows}
        if connection:
            rows = [row for row in rows if (row["grant_id"] or "panel") == connection]
            period_rows = [row for row in period_rows if (row["grant_id"] or "panel") == connection]
        windows = sorted({row["window_key"] for row in period_rows if row.get("window_key")})
        if session:
            rows = [row for row in rows if (row.get("window_key") or "uncorrelated") == session]
        expiry_seconds = min(CACHE_SECONDS, (midnight + timedelta(days=1)).timestamp() - upper)
        for row in rows:
            row["token_usage"] = None
            encoded = row.pop("usage_json", None)
            if encoded:
                try:
                    row["token_usage"] = _safe_usage(json.loads(encoded))
                except (AttributeError, TypeError, ValueError):
                    collection_unavailable = True
            if row.get("usage_created") is not None:
                expiry_seconds = min(expiry_seconds, max(0, row["usage_created"] + RETENTION_SECONDS - retention_now))
            if now is None:
                expiry_seconds = min(expiry_seconds, max(0, row["started"] + RETENTION_SECONDS - upper))
        period_rows = [row for row in rows if row["started"] >= lower]
        total = summarize(period_rows, cache_read_percent=cache_read_percent, reference_model=reference_model)
        total["scope"] = "retained_authorized_project_activity"
        def bucket_for(timestamp):
            local = datetime.fromtimestamp(timestamp, timezone)
            if period == "today":
                return lower + int((timestamp - lower) // 3600) * 3600
            return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        context, context_buckets = context_usage(rows, lower, upper, bucket_for,
                                                cache_read_percent=cache_read_percent,
                                                reference_model=reference_model,
                                                collection_unavailable=collection_unavailable)
        total["context_estimate"] = context
        buckets = defaultdict(list)
        for row in period_rows:
            buckets[bucket_for(row["started"])].append(row)
        trend = []
        for bucket in sorted(set(buckets) | set(context_buckets)):
            point = summarize(buckets[bucket], cache_read_percent=cache_read_percent, reference_model=reference_model)
            point["scope"] = total["scope"]
            point["context_estimate"] = context_buckets.get(bucket)
            trend.append({"started": bucket, **point})
        result = {
            "schema_version": 3, "authority_key": authority_key,
            "period": {"key": period, "start": lower, "end": upper,
                       "timezone": str(timezone), "bucket": "hour" if period == "today" else "day"},
            "filters": {"project": project, "connection": connection, "session": session},
            "options": {
                "projects": [{"id": item["id"], "label": item["alias"]} for item in projects],
                "connections": [{"id": key, "label": label} for key, label in sorted(connections.items())],
                "sessions": [{"id": key, "label": f"匿名窗口 {index + 1}"} for index, key in enumerate(windows)] +
                    [{"id": "uncorrelated", "label": "未关联窗口"}],
            },
            "summary": total, "trend": trend,
            "coverage": {"scope": total["scope"], "complete_history": False,
                "activity_row_limit": MAX_ROWS, "estimate_retention_days": RETENTION_SECONDS // 86400,
                "collection_unavailable": collection_unavailable,
                "oldest_visible_activity": min((row["started"] for row in period_rows), default=None),
                "context_warmup_attempts": len(rows) - len(period_rows),
                "context_version": CONTEXT_VERSION},
        }
        if not collection_unavailable and expiry_seconds > 0:
            cache[cache_key] = (time.monotonic() + expiry_seconds, deepcopy(result), time.time() + expiry_seconds)
            cache.move_to_end(cache_key)
        for key in list(cache):
            if cache[key][0] <= time.monotonic():
                cache.pop(key, None)
        while len(cache) > CACHE_ENTRIES:
            cache.popitem(last=False)
        return result
