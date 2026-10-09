"""Payload-only usage metadata bound to authorized MCP activity IDs."""
from __future__ import annotations

import json
import time

from shared.token_estimate import summarize
from hub import iam

MAX_ROWS = 10_000
RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_RECORD_BYTES = 4096
NOTE = ("仅估算已授权项目工具的参数和返回文本；按返回页统计，不是账单或完整对话 token。"
        "不含模型思考、系统提示、工具目录、缓存及图片/文件传输，宿主实际用量未知。"
        "区间是未经模型校准的启发式范围，不是误差保证；历史缺失显示未知。")


def activity_actor_scope(store, principal, alias=""):
    """Shared live identity boundary for activity pages and aggregate views."""
    if principal.grant_id:
        return alias + "grant_id=?", [principal.grant_id]
    if iam.installed(store) and not principal.instance_admin:
        return ("(" + alias + "actor=? OR " + alias +
                "grant_id IN (SELECT id FROM grants WHERE user_id=? AND space_id=?))",
                [principal.actor, principal.user_id, principal.space_id])
    if not iam.installed(store) and not principal.admin:
        return alias + "grant_id=?", [principal.grant_id]
    return "1=1", []


class MCPUsage:
    def __init__(self, store):
        self.store = store
        store.execute("""CREATE TABLE IF NOT EXISTS mcp_usage (
            activity_id INTEGER PRIMARY KEY REFERENCES mcp_activity(id) ON DELETE CASCADE,
            created REAL NOT NULL, metrics TEXT NOT NULL CHECK(length(metrics)<=4096))""")
        store.execute("CREATE INDEX IF NOT EXISTS mcp_usage_created ON mcp_usage(created)")
        self.prune()

    def prune(self):
        # The row quota applies on every insert, including many requests in one
        # timestamp. The time quota and parent existence are checked on reads too.
        self.store.execute("DELETE FROM mcp_usage WHERE created<? OR activity_id NOT IN (SELECT id FROM mcp_activity)",
                           (time.time() - RETENTION_SECONDS,))
        self.store.execute("DELETE FROM mcp_usage WHERE activity_id NOT IN (SELECT activity_id FROM mcp_usage ORDER BY activity_id DESC LIMIT ?)",
                           (MAX_ROWS,))

    def record(self, activity_id, metrics):
        encoded = json.dumps(metrics, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if len(encoded) > MAX_RECORD_BYTES:
            raise ValueError("Usage record exceeds metadata quota")
        # One materialized response per activity. Repeated finish callbacks do
        # not create rows; retries received as new calls have new activity IDs.
        self.store.execute("INSERT OR IGNORE INTO mcp_usage(activity_id,created,metrics) SELECT id,?,? FROM mcp_activity WHERE id=?",
                           (time.time(), encoded, activity_id))
        self.prune()

    def enrich(self, rows):
        # Only IDs already projected through activity's current IAM filters.
        # Reads stay read-only. Retention is enforced in the SELECT as well as
        # insert-time pruning, including after a long idle interval.
        identifiers = [row["id"] for row in rows]
        measured = {}
        for offset in range(0, len(identifiers), 400):
            batch = identifiers[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            for item in self.store.all(f"SELECT activity_id,metrics FROM mcp_usage WHERE activity_id IN ({placeholders}) AND created>=?",
                                       (*batch, time.time() - RETENTION_SECONDS)):
                measured[item["activity_id"]] = json.loads(item["metrics"])
        for row in rows:
            row["token_usage"] = measured.get(row["id"])
        return summarize(rows)
