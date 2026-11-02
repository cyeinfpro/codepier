"""Persistent revisions for bounded, read-only dashboard snapshots.

Triggers cover writers using another SQLite connection too. Ordinary activity
updates do not change visibility: a metric may be stale for the documented
refresh interval, while reassigning its authorization fields invalidates every
cached snapshot immediately. No payload or identity is stored in revisions.
"""
from __future__ import annotations


def install(store):
    with store.transaction():
        store.db.execute("""CREATE TABLE IF NOT EXISTS token_dashboard_revision (
            id INTEGER PRIMARY KEY CHECK(id=1),
            data_revision INTEGER NOT NULL DEFAULT 0,
            visibility_revision INTEGER NOT NULL DEFAULT 0)""")
        store.db.execute("INSERT OR IGNORE INTO token_dashboard_revision(id) VALUES(1)")
        tables = {row["name"] for row in store.all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('mcp_activity','mcp_usage')")}
        for table in sorted(tables):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                name = f"token_dashboard_{table}_{operation.lower()}"
                store.db.execute(f"""CREATE TRIGGER IF NOT EXISTS {name}
                    AFTER {operation} ON {table} BEGIN
                    UPDATE token_dashboard_revision SET data_revision=data_revision+1 WHERE id=1;
                    END""")
        if "mcp_activity" in tables:
            store.db.execute("""CREATE TRIGGER IF NOT EXISTS token_dashboard_visibility
                AFTER UPDATE OF project,root,device,grant_id,actor ON mcp_activity
                WHEN old.project IS NOT new.project OR old.root IS NOT new.root
                    OR old.device IS NOT new.device OR old.grant_id IS NOT new.grant_id
                    OR old.actor IS NOT new.actor
                BEGIN
                UPDATE token_dashboard_revision SET visibility_revision=visibility_revision+1 WHERE id=1;
                END""")
            store.db.execute("""CREATE INDEX IF NOT EXISTS mcp_activity_mapping_started
                ON mcp_activity(project,root,device,started DESC,id DESC)""")


def revision(store):
    row = store.one("SELECT data_revision,visibility_revision FROM token_dashboard_revision WHERE id=1")
    if row is None:
        raise RuntimeError("Missing token dashboard revision record")
    return row["data_revision"], row["visibility_revision"]
