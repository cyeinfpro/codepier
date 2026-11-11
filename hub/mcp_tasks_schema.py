"""Additive task bindings; operation completion is frozen in the same transaction."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_tasks (
    task_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
    space_id TEXT NOT NULL REFERENCES spaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    grant_id TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
    renderer_version INTEGER NOT NULL DEFAULT 1 CHECK(renderer_version=1),
    created REAL NOT NULL, terminal_operation TEXT
);
CREATE INDEX IF NOT EXISTS mcp_tasks_owner ON mcp_tasks(space_id,owner_user_id,grant_id);
"""
TERMINAL = "'succeeded','failed','cancelled','needs_review','interrupted'"


def migrate(db):
    # Historical fixture/upgrade stores may deliberately stop before IAM.
    # Do not install triggers referencing columns that the IAM rebuild has not
    # introduced yet. Normal Store startup calls us after that migration.
    columns = {entry[1] for entry in db.execute("PRAGMA table_info(operations)")}
    if not {"space_id", "owner_user_id"} <= columns:
        version = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
        if int(version) < 8:
            return
        raise RuntimeError("MCP Tasks requires the completed IAM migration")
    row = db.execute("SELECT value FROM meta WHERE key='mcp_tasks_schema'").fetchone()
    if row and row[0] != "1":
        raise RuntimeError("Unsupported MCP Tasks schema")
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    task_columns = {entry[1] for entry in db.execute("PRAGMA table_info(mcp_tasks)")}
    if "terminal_result" not in task_columns:
        db.execute("ALTER TABLE mcp_tasks ADD COLUMN terminal_result TEXT")
    # Replace only the trigger definition. Existing frozen receipts remain intact.
    db.execute("DROP TRIGGER IF EXISTS mcp_tasks_terminal")
    db.execute("""CREATE TRIGGER IF NOT EXISTS mcp_tasks_binding_insert BEFORE INSERT ON mcp_tasks
        WHEN NOT EXISTS(SELECT 1 FROM operations o WHERE o.id=NEW.task_id AND o.tool='exec'
            AND o.space_id=NEW.space_id AND o.owner_user_id=NEW.owner_user_id AND o.grant_id=NEW.grant_id)
        BEGIN SELECT RAISE(ABORT,'invalid MCP task binding'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS mcp_tasks_binding_immutable
        BEFORE UPDATE OF task_id,space_id,owner_user_id,grant_id,renderer_version,created ON mcp_tasks
        BEGIN SELECT RAISE(ABORT,'MCP task binding is immutable'); END""")
    # A task must never regress to working after a late Agent recovery. Freeze
    # the original receipt atomically, even if no client polls its terminal state.
    db.execute(f"""CREATE TRIGGER IF NOT EXISTS mcp_tasks_terminal
        AFTER UPDATE OF state,result ON operations
        WHEN NEW.state IN ({TERMINAL})
        BEGIN UPDATE mcp_tasks SET terminal_operation=json_object(
            'operation_id',NEW.id,'state',NEW.state,'result',NULL,
            'error',NEW.error,'updated',NEW.updated),terminal_result=NEW.result
        WHERE task_id=NEW.id AND terminal_operation IS NULL; END""")
    for column in ("terminal_operation", "terminal_result"):
        db.execute("DROP TRIGGER IF EXISTS " + immutable_trigger_name(column))
        db.execute(immutable_trigger_sql(column))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('mcp_tasks_schema','1')")


def immutable_trigger_name(column):
    if column not in {"terminal_operation", "terminal_result"}:
        raise ValueError("Unsupported frozen task column")
    return (
        "mcp_tasks_terminal_immutable"
        if column == "terminal_operation"
        else "mcp_tasks_terminal_result_immutable"
    )


def immutable_trigger_sql(column, *, exception=None):
    """Ordinary persistent invariants use only built-in SQLite operations."""
    name = immutable_trigger_name(column)
    allowed = (" AND NOT (" + exception + ")") if exception else ""
    return f"""CREATE TRIGGER {name} BEFORE UPDATE OF {column} ON mcp_tasks
        WHEN OLD.terminal_operation IS NOT NULL AND NEW.{column} IS NOT OLD.{column}{allowed}
        BEGIN SELECT RAISE(ABORT,'MCP task terminal state or result is immutable'); END"""
