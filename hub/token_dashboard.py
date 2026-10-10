"""Read-only, current-authority summaries of retained MCP tool-text estimates."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal

from hub import iam
from hub.mcp_usage import MAX_ROWS, RETENTION_SECONDS, activity_actor_scope
from hub.principal import refresh_principal
from shared.token_estimate import summarize
from shared.token_cost import DEFAULT_CACHE_READ_PERCENT, DEFAULT_REFERENCE_MODEL, MODELS, pricing
from shared.util import DevError

Period = Literal["today", "7d", "30d"]


def token_dashboard(runtime, principal, timezone, *, period: Period = "today",
                    project: str = "", connection: str = "", session: str = "",
                    cache_read_percent: int = DEFAULT_CACHE_READ_PERCENT,
                    reference_model: str = DEFAULT_REFERENCE_MODEL,
                    now: float | None = None):
    """Resolve authority BEFORE selecting rows, and never aggregate the global table.

    All filters and options derive from those same authorized rows. Activity IDs,
    not server operation IDs, identify materialized wire attempts. No prompt,
    response, request metadata, or inferred host identity leaves this service.
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
    with iam.read_scope(store):
        principal = refresh_principal(store, principal)
        projects = runtime.list_projects(principal)
        if project and project not in {item["id"] for item in projects}:
            raise DevError("PROJECT_NOT_FOUND", "未找到已授权项目", 404)
        finish = datetime.fromtimestamp(now, timezone) if now is not None else datetime.now(timezone)
        start = finish.replace(hour=0, minute=0, second=0, microsecond=0)
        start -= timedelta(days={"today": 0, "7d": 6, "30d": 29}[period])
        lower, upper = start.timestamp(), finish.timestamp()
        actor_clause, actor_args = activity_actor_scope(store, principal, "a.")
        authority_key = hashlib.sha256(json.dumps([
            principal.user_id, principal.space_id, principal.grant_id,
            principal.admin, principal.instance_admin, sorted(principal.scopes),
            sorted((item["id"], item["root"], item["device_id"]) for item in projects),
        ], separators=(",", ":")).encode()).hexdigest()
        visible_projects = [item for item in projects if not project or item["id"] == project]
        rows = []
        # Chunk project triples below SQLite's conservative parameter limit.
        # Only already-authorized, CURRENT root/device mappings enter this SQL.
        for offset in range(0, len(visible_projects), 100):
            batch = visible_projects[offset:offset + 100]
            mappings = " OR ".join("(a.project=? AND a.root=? AND a.device=?)" for _ in batch)
            values = [value for item in batch for value in (item["id"], item["root"], item["device_id"])]
            where = f"({mappings}) AND a.started>=? AND a.started<=? AND ({actor_clause})"
            # A grant from another space never grants visibility, even to an
            # instance admin inspecting a project in the current space.
            where += " AND (a.grant_id IS NULL OR EXISTS (SELECT 1 FROM grants sg WHERE sg.id=a.grant_id AND sg.space_id=?))"
            values.extend((lower, upper, *actor_args, principal.space_id))
            rows.extend(store.all(
                "SELECT a.id,a.project,a.grant_id,a.window_key,a.started,a.operation_id,g.label AS connection_label "
                "FROM mcp_activity a LEFT JOIN grants g ON g.id=a.grant_id WHERE " + where +
                " ORDER BY a.id DESC LIMIT ?", (*values, MAX_ROWS)))
            # Keep temporary accumulation bounded even before source pruning.
            rows = sorted(rows, key=lambda row: row["id"], reverse=True)[:MAX_ROWS]
        # The activity store is globally bounded, and so is this read view.
        rows = sorted({row["id"]: row for row in rows}.values(), key=lambda row: row["id"], reverse=True)[:MAX_ROWS]
        connections = {row["grant_id"] or "panel": row.get("connection_label") or "管理面板" for row in rows}
        if connection:
            rows = [row for row in rows if (row["grant_id"] or "panel") == connection]
        windows = sorted({row["window_key"] for row in rows if row.get("window_key")})
        if session:
            rows = [row for row in rows if (row.get("window_key") or "uncorrelated") == session]
        collection_unavailable = runtime.integrations.usage_metrics is None
        for row in rows:
            row["token_usage"] = None
        try:
            if runtime.integrations.usage_metrics:
                runtime.integrations.usage_metrics.enrich(rows)
        except Exception:
            collection_unavailable = True
            for row in rows:
                row["token_usage"] = None
        total = summarize(rows, cache_read_percent=cache_read_percent, reference_model=reference_model)
        total["scope"] = "retained_authorized_project_activity"
        buckets = defaultdict(list)
        for row in rows:
            local = datetime.fromtimestamp(row["started"], timezone)
            if period == "today":
                bucket = lower + int((row["started"] - lower) // 3600) * 3600
            else:
                bucket = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            buckets[bucket].append(row)
        trend = []
        for bucket, items in sorted(buckets.items()):
            point = summarize(items, cache_read_percent=cache_read_percent, reference_model=reference_model)
            point["scope"] = total["scope"]
            trend.append({"started": bucket, **point})
        return {
            "schema_version": 2, "authority_key": authority_key, "period": {"key": period, "start": lower, "end": upper,
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
                "oldest_visible_activity": min((row["started"] for row in rows), default=None)},
        }
