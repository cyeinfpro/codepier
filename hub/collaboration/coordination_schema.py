"""Additive durable goal/work/attempt schema, independent of monitor jobs."""


def migrate(db):
    statements = """
    CREATE TABLE IF NOT EXISTS coordination_goals (
        id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id), spec TEXT NOT NULL,
        digest TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'proposed', version INTEGER NOT NULL DEFAULT 1,
        approval_id TEXT, expires_at REAL, steps INTEGER NOT NULL DEFAULT 0,
        messages INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS coordination_goals_room ON coordination_goals(room_id,conversation_id,created);
    CREATE TABLE IF NOT EXISTS coordination_approvals (
        id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES coordination_goals(id),
        goal_version INTEGER NOT NULL, digest TEXT NOT NULL, spec TEXT NOT NULL,
        principal TEXT NOT NULL, created REAL NOT NULL, expires_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS coordination_work (
        id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES coordination_goals(id),
        approval_id TEXT NOT NULL REFERENCES coordination_approvals(id), project_id TEXT NOT NULL,
        objective TEXT NOT NULL, acceptance TEXT NOT NULL, responsibility TEXT NOT NULL DEFAULT '',
        assignee_grant_id TEXT NOT NULL, dependencies TEXT NOT NULL DEFAULT '[]',
        required_capabilities TEXT NOT NULL DEFAULT '["read"]',
        state TEXT NOT NULL DEFAULT 'queued', version INTEGER NOT NULL DEFAULT 1,
        attempt INTEGER NOT NULL DEFAULT 0, fencing_token INTEGER NOT NULL DEFAULT 0,
        lease_until REAL, result TEXT, reason_code TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, updated REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS coordination_work_goal ON coordination_work(goal_id,created,id);
    CREATE TABLE IF NOT EXISTS coordination_attempts (
        work_item_id TEXT NOT NULL REFERENCES coordination_work(id), attempt INTEGER NOT NULL,
        fencing_token INTEGER NOT NULL, grant_id TEXT NOT NULL, created REAL NOT NULL,
        PRIMARY KEY(work_item_id,attempt), UNIQUE(work_item_id,fencing_token)
    );
    CREATE TABLE IF NOT EXISTS coordination_operations (
        operation_id TEXT PRIMARY KEY REFERENCES operations(id),
        goal_id TEXT NOT NULL REFERENCES coordination_goals(id),
        work_item_id TEXT NOT NULL REFERENCES coordination_work(id), approval_id TEXT NOT NULL,
        attempt INTEGER NOT NULL, fencing_token INTEGER NOT NULL, project_id TEXT NOT NULL,
        grant_id TEXT NOT NULL, request_digest TEXT NOT NULL, cancel_note TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS coordination_operations_goal ON coordination_operations(goal_id,work_item_id,attempt);
    CREATE TABLE IF NOT EXISTS coordination_messages (
        id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES coordination_goals(id),
        approval_id TEXT NOT NULL, author_grant_id TEXT NOT NULL, body TEXT NOT NULL,
        mention_grant_ids TEXT NOT NULL, provenance_project_ids TEXT NOT NULL,
        created REAL NOT NULL
    )
    """
    for statement in statements.split(';'):
        if statement.strip():
            db.execute(statement)
    columns = {row[1] for row in db.execute('PRAGMA table_info(coordination_work)')}
    if 'required_capabilities' not in columns:
        db.execute('ALTER TABLE coordination_work ADD COLUMN required_capabilities TEXT NOT NULL DEFAULT \'["read"]\'')
    db.execute("""CREATE TRIGGER IF NOT EXISTS coordination_approval_immutable
        BEFORE UPDATE ON coordination_approvals
        BEGIN SELECT RAISE(ABORT,'goal approvals are immutable'); END""")
