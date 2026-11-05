"""Bounded panel-only queue projections; no request decryption or execution."""
from __future__ import annotations

import time

from fastapi import Request

from hub import iam
from hub.call_log import public_row
from hub.db_worker import database_endpoint
from shared.util import DevError

ACTIVE = ("queued", "running", "reconnecting", "cancelling", "unknown")


def queue_rows(store, runtime, principal, limit=100):
    clause, args = iam.private_sql(principal, "o.")
    if not principal.admin:
        clause += " AND (o.project_id IS NULL OR o.project_id IN (%s))" % (
            ",".join("?" for _ in principal.projects) or "NULL")
        args.extend(principal.projects)
    rows = store.all(
        """SELECT o.id,o.project_id,o.device_id,o.tool,o.actor,o.state,o.created,
        o.updated,o.args_summary,o.error,o.attempts,o.accepted_at,o.deadline,
        o.cancel_requested,o.transport_error,p.alias,d.name AS device_name
        FROM operations o LEFT JOIN projects p ON p.id=o.project_id
        LEFT JOIN devices d ON d.id=o.device_id WHERE """ + clause +
        " AND o.state IN (?,?,?,?,?) ORDER BY o.created,o.id LIMIT ?",
        (*args, *ACTIVE, 1000))
    observed = time.time()
    result = []
    for row in rows:
        # SQL is only a coarse tenant/owner filter. Apply the full originating
        # operation policy (including computer scopes and project roles) before
        # projecting any metadata, then independently filter each blocker.
        try:
            runtime.operation_row(row['id'], principal, status_only=True)
        except DevError as error:
            if error.status in {403, 404}:
                continue
            raise
        item = public_row(row, observed)
        item.update(runtime.queue_annotations(row, principal))
        item["can_cancel"] = cancellable(runtime, principal, row)
        result.append(item)
        if len(result) > limit:
            break
    return {"operations": result[:limit], "truncated": len(result) > limit,
            "observed_at": observed, "scope": "visible_space_operations",
            "diagnostics_available": principal.instance_admin}


def cancellable(runtime, principal, row):
    from hub.runtime import DEVICE_ACTIONS, PROCESS_TOOLS
    if row["state"] not in ACTIVE or row.get("cancel_requested") or row["tool"] in DEVICE_ACTIONS:
        return False
    reads = {"read", "fs_tree", "fs_read", "fs_read_many", "fs_search", "project_context"}
    if row["tool"] not in PROCESS_TOOLS | reads and row.get("attempts"):
        return False
    try:
        if row.get("project_id"):
            runtime.authorize(principal, "execute", project_id=row["project_id"])
    except DevError:
        return False
    return True


def register_queue_routes(router, store, runtime, auth):
    @router.get("/api/operations/queue")
    @database_endpoint(store, read_only=True)
    def queue(request: Request):
        with store.transaction(immediate=False), iam.read_scope(store):
            principal = auth.panel(request)
            return queue_rows(store, runtime, principal)

    @router.get("/api/queue-diagnostics")
    @database_endpoint(store, read_only=True)
    def diagnostics(request: Request):
        with store.transaction(immediate=False), iam.read_scope(store):
            principal = auth.panel(request)
            if not principal.instance_admin:
                raise DevError("FORBIDDEN", "仅实例管理员可读取实例级队列诊断", 403)
            from hub.queue_contention import queue_contention_report
            return {"scope": "hub_instance", "contention": queue_contention_report(store),
                    "metrics": store.stats_snapshot()}
