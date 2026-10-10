"""Additive tables on the existing Store connection and migration transaction.

Project/grant IDs remain tombstones after unmapping/revocation. Historical IDs
are never authority: every access resolves the current project and grant.
"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS collaboration_rooms (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
 owner_user_id TEXT NOT NULL REFERENCES users(id), project_id TEXT NOT NULL,
 environment_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','paused')),
 version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
 UNIQUE(space_id,owner_user_id,project_id,environment_id)
);
CREATE TABLE IF NOT EXISTS collaboration_agents (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 label TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('work_cloud','dot')),
 queue TEXT NOT NULL CHECK(queue IN ('work-analysis','dot-coordination')),
 grant_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 version INTEGER NOT NULL DEFAULT 1, expires_at REAL NOT NULL, created REAL NOT NULL,
 UNIQUE(room_id,grant_id,queue)
);
CREATE INDEX IF NOT EXISTS collaboration_agents_room ON collaboration_agents(room_id,created,id);
CREATE TABLE IF NOT EXISTS collaboration_join_slots (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 label TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('work_cloud','dot')),
 state TEXT NOT NULL DEFAULT 'invited' CHECK(state IN ('invited','registered','revoked')),
 version INTEGER NOT NULL DEFAULT 1, join_code TEXT NOT NULL UNIQUE,
 code_expires_at REAL NOT NULL, expires_at REAL NOT NULL,
 grant_id TEXT NOT NULL DEFAULT '', principal TEXT NOT NULL DEFAULT '{}',
 joined_at REAL, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS collaboration_join_slots_room ON collaboration_join_slots(room_id,created,id);
CREATE TABLE IF NOT EXISTS collaboration_join_routes (
 subscription_id TEXT PRIMARY KEY REFERENCES mcp_event_subscriptions(id),
 slot_id TEXT NOT NULL REFERENCES collaboration_join_slots(id),
 endpoint_digest TEXT NOT NULL, credential_digest TEXT NOT NULL, confirmed_event_id TEXT NOT NULL DEFAULT '',
 test_after_seq INTEGER NOT NULL DEFAULT 0, confirmed_at REAL, confirmed_by TEXT NOT NULL DEFAULT '', created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS collaboration_join_routes_slot ON collaboration_join_routes(slot_id);
CREATE TABLE IF NOT EXISTS collaboration_messages (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 thread_id TEXT NOT NULL, author TEXT NOT NULL, origin TEXT NOT NULL,
 source_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
 state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 goal_id TEXT, created REAL NOT NULL, UNIQUE(room_id,origin,author,source_id)
);
CREATE INDEX IF NOT EXISTS collaboration_messages_room ON collaboration_messages(room_id,created DESC,id DESC);
CREATE TABLE IF NOT EXISTS collaboration_goals (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 source_message_id TEXT NOT NULL UNIQUE REFERENCES collaboration_messages(id),
 request TEXT NOT NULL, acceptance TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
 version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS collaboration_goals_room ON collaboration_goals(room_id,created DESC,id DESC);
CREATE TABLE IF NOT EXISTS collaboration_jobs (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 goal_id TEXT REFERENCES collaboration_goals(id), incident_id TEXT,
 kind TEXT NOT NULL CHECK(kind IN ('analyze_incident','propose_monitor_plan','summarize_result')),
 assignee_agent_id TEXT NOT NULL REFERENCES collaboration_agents(id),
 target_grant_id TEXT NOT NULL, queue TEXT NOT NULL,
 next_assignee_agent_id TEXT NOT NULL DEFAULT '', parent_job_id TEXT,
 depth INTEGER NOT NULL DEFAULT 0 CHECK(depth BETWEEN 0 AND 4),
 hops INTEGER NOT NULL DEFAULT 0 CHECK(hops BETWEEN 0 AND 8),
 business_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'queued',
 version INTEGER NOT NULL DEFAULT 1, attempt INTEGER NOT NULL DEFAULT 0,
 fencing_token INTEGER NOT NULL DEFAULT 0, lease_grant_id TEXT, lease_until REAL,
 deadline_at REAL NOT NULL, not_before REAL NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 3),
 tool_calls INTEGER NOT NULL DEFAULT 0, context TEXT NOT NULL,
 plan_version INTEGER, reason_code TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL, updated REAL NOT NULL,
 CHECK(state IN ('queued','leased','running','blocked','retry_wait','succeeded','failed','cancelled','expired','dead_letter'))
);
CREATE INDEX IF NOT EXISTS collaboration_jobs_queue ON collaboration_jobs(room_id,queue,state,not_before,created);
CREATE INDEX IF NOT EXISTS collaboration_jobs_lease ON collaboration_jobs(state,lease_until,deadline_at);
CREATE TABLE IF NOT EXISTS collaboration_attempts (
 job_id TEXT NOT NULL REFERENCES collaboration_jobs(id), attempt INTEGER NOT NULL,
 fencing_token INTEGER NOT NULL, grant_id TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(job_id,attempt), UNIQUE(job_id,fencing_token)
);
CREATE TABLE IF NOT EXISTS collaboration_results (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES collaboration_jobs(id),
 room_id TEXT NOT NULL REFERENCES collaboration_rooms(id), attempt INTEGER NOT NULL,
 body TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL, UNIQUE(job_id,attempt)
);
CREATE TABLE IF NOT EXISTS collaboration_late_results (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES collaboration_jobs(id),
 attempt INTEGER NOT NULL, fencing_token INTEGER NOT NULL, digest TEXT NOT NULL,
 body TEXT NOT NULL, created REAL NOT NULL, UNIQUE(job_id,attempt,fencing_token,digest)
);
CREATE TABLE IF NOT EXISTS collaboration_consumptions (
 room_id TEXT NOT NULL REFERENCES collaboration_rooms(id), grant_id TEXT NOT NULL,
 result_id TEXT NOT NULL REFERENCES collaboration_results(id), created REAL NOT NULL,
 PRIMARY KEY(room_id,grant_id,result_id)
);
CREATE TABLE IF NOT EXISTS collaboration_idempotency (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 digest TEXT NOT NULL, response TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collaboration_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS collaboration_audit_room ON collaboration_audit(room_id,id);
CREATE TABLE IF NOT EXISTS collaboration_secrets (id TEXT PRIMARY KEY, secret TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monitor_probes (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 label TEXT NOT NULL, config TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_plan_bindings (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL UNIQUE REFERENCES collaboration_rooms(id),
 latest_version INTEGER NOT NULL DEFAULT 0, active_version INTEGER,
 version INTEGER NOT NULL DEFAULT 1, approval_id TEXT,
 state TEXT NOT NULL DEFAULT 'draft', next_due REAL NOT NULL DEFAULT 0,
 collector_epoch TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0,
 collection_fence INTEGER NOT NULL DEFAULT 0, collection_lease_until REAL,
 last_collected REAL, last_evaluated REAL, status TEXT NOT NULL DEFAULT 'not_started'
);
CREATE TABLE IF NOT EXISTS monitor_plans (
 plan_id TEXT NOT NULL REFERENCES monitor_plan_bindings(id), version INTEGER NOT NULL,
 config TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(plan_id,version)
);
CREATE TABLE IF NOT EXISTS monitor_approvals (
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES monitor_plan_bindings(id),
 plan_version INTEGER NOT NULL, digest TEXT NOT NULL,
 actor TEXT NOT NULL, principal TEXT NOT NULL, expires_at REAL NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_samples (
 id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 plan_id TEXT NOT NULL, plan_version INTEGER NOT NULL, probe_id TEXT NOT NULL REFERENCES monitor_probes(id),
 collector_epoch TEXT NOT NULL, sequence INTEGER NOT NULL, collected_at REAL NOT NULL,
 received_at REAL NOT NULL, available INTEGER NOT NULL, http_error INTEGER NOT NULL,
 latency_ms REAL, quality TEXT NOT NULL,
 UNIQUE(probe_id,collector_epoch,sequence)
);
CREATE INDEX IF NOT EXISTS monitor_samples_window ON monitor_samples(plan_id,plan_version,probe_id,collected_at);
CREATE TABLE IF NOT EXISTS monitor_rule_state (
 plan_id TEXT NOT NULL, plan_version INTEGER NOT NULL, rule_id TEXT NOT NULL,
 watermark REAL NOT NULL DEFAULT 0, opening INTEGER NOT NULL DEFAULT 0,
 closing INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'insufficient_data',
 PRIMARY KEY(plan_id,plan_version,rule_id)
);
CREATE TABLE IF NOT EXISTS monitor_incidents (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 fingerprint TEXT NOT NULL, rule_id TEXT NOT NULL, probe_id TEXT NOT NULL,
 plan_id TEXT NOT NULL, plan_version INTEGER NOT NULL, episode INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL, severity TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 evidence_id TEXT, analysis_state TEXT NOT NULL DEFAULT 'not_requested',
 suppressed INTEGER NOT NULL DEFAULT 0, opened_at REAL NOT NULL, updated REAL NOT NULL, resolved_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS monitor_incidents_open ON monitor_incidents(room_id,fingerprint) WHERE state != 'resolved';
CREATE INDEX IF NOT EXISTS monitor_incidents_room ON monitor_incidents(room_id,opened_at DESC,id);
CREATE TABLE IF NOT EXISTS monitor_evidence (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 incident_id TEXT, body TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL,
 expires_at REAL NOT NULL, redaction_version INTEGER NOT NULL DEFAULT 1,
 classification TEXT NOT NULL DEFAULT 'aggregate_probe'
);
CREATE TABLE IF NOT EXISTS mcp_event_subscriptions (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
 principal TEXT NOT NULL, grant_id TEXT NOT NULL, name TEXT NOT NULL,
 arguments TEXT NOT NULL, secret TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
 expires_at REAL NOT NULL, verified_until REAL NOT NULL, key_digest TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, scan_seq INTEGER NOT NULL DEFAULT 0,
 ack_seq INTEGER NOT NULL DEFAULT 0, last_accepted REAL, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS mcp_event_subscriptions_scope ON mcp_event_subscriptions(room_id,grant_id,state);
CREATE TABLE IF NOT EXISTS mcp_event_outbox (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 room_id TEXT NOT NULL REFERENCES collaboration_rooms(id), name TEXT NOT NULL,
 object_id TEXT NOT NULL, object_version INTEGER NOT NULL, target_grant_id TEXT NOT NULL DEFAULT '',
 queue TEXT NOT NULL DEFAULT '', data TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(name,object_id,object_version,target_grant_id)
);
CREATE INDEX IF NOT EXISTS mcp_event_outbox_room ON mcp_event_outbox(room_id,seq);
CREATE TABLE IF NOT EXISTS mcp_event_deliveries (
 id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES mcp_event_subscriptions(id),
 event_id TEXT NOT NULL REFERENCES mcp_event_outbox(id), event_seq INTEGER NOT NULL,
 body TEXT, state TEXT NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, lease_until REAL, fence INTEGER NOT NULL DEFAULT 0,
 status_code INTEGER, reason_code TEXT NOT NULL DEFAULT '', created REAL NOT NULL, accepted_at REAL,
 UNIQUE(subscription_id,event_id)
);
CREATE INDEX IF NOT EXISTS mcp_event_deliveries_ready ON mcp_event_deliveries(subscription_id,state,event_seq,next_at);
"""


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(projects)')}
    if not {'space_id', 'owner_user_id'} <= columns:
        if int(db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]) < 8:
            return
        raise RuntimeError('Collaboration requires the completed IAM migration')
    version = db.execute("SELECT value FROM meta WHERE key='collaboration_schema'").fetchone()
    if version and version[0] not in {'1', '2', '3'}:
        raise RuntimeError('Unsupported collaboration schema')
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    db.execute("""CREATE TRIGGER IF NOT EXISTS monitor_plan_immutable BEFORE UPDATE ON monitor_plans
        BEGIN SELECT RAISE(ABORT,'monitor plan versions are immutable'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS collaboration_result_immutable BEFORE UPDATE ON collaboration_results
        BEGIN SELECT RAISE(ABORT,'analysis results are immutable'); END""")
    migrate_chatroom(db)
    from hub.collaboration.conversations import migrate as migrate_conversations
    migrate_conversations(db)
    from hub.collaboration.coordination_schema import migrate as migrate_coordination
    migrate_coordination(db)
    from hub.collaboration.delegation import migrate as migrate_delegation
    migrate_delegation(db)
    from hub.collaboration.dots import migrate as migrate_dots
    migrate_dots(db)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('collaboration_schema','3')")

def migrate_chatroom(db):
    """Forward-only additive upgrade; no existing job, join or approval is rebuilt.

    Changes are retained without expiry in v2. Signed cursors can replay the
    complete log; future retention must introduce an explicit reset boundary.
    """
    import json
    columns = {row[1] for row in db.execute('PRAGMA table_info(collaboration_messages)')}
    for name, declaration in [('server_sequence', 'INTEGER NOT NULL DEFAULT 0'),
                              ('thread_root_id', "TEXT NOT NULL DEFAULT ''"),
                              ('reply_to_id', "TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:
            db.execute(f'ALTER TABLE collaboration_messages ADD COLUMN {name} {declaration}')
    rooms = {row[1] for row in db.execute('PRAGMA table_info(collaboration_rooms)')}
    for name, declaration in [('title', "TEXT NOT NULL DEFAULT ''"), ('topic', "TEXT NOT NULL DEFAULT ''"),
                              ('chat_history_sequence', 'INTEGER NOT NULL DEFAULT 0')]:
        if name not in rooms:
            db.execute(f'ALTER TABLE collaboration_rooms ADD COLUMN {name} {declaration}')
    db.execute('''CREATE TABLE IF NOT EXISTS collaboration_room_changes (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
        kind TEXT NOT NULL, object_id TEXT NOT NULL, version INTEGER NOT NULL, created REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS collaboration_changes_room ON collaboration_room_changes(room_id,sequence)')
    db.execute('''CREATE TABLE IF NOT EXISTS collaboration_read_cursors (
        room_id TEXT NOT NULL REFERENCES collaboration_rooms(id), user_id TEXT NOT NULL,
        sequence INTEGER NOT NULL, updated REAL NOT NULL, PRIMARY KEY(room_id,user_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS collaboration_message_writers (
        room_id TEXT NOT NULL REFERENCES collaboration_rooms(id), grant_id TEXT NOT NULL,
        enabled INTEGER NOT NULL, expires_at REAL NOT NULL, principal TEXT NOT NULL,
        updated REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(room_id,grant_id))''')
    writers = {row[1] for row in db.execute('PRAGMA table_info(collaboration_message_writers)')}
    if 'version' not in writers:
        db.execute('ALTER TABLE collaboration_message_writers ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
    mapped, unresolved = 0, 0
    for room in db.execute('SELECT id FROM collaboration_rooms').fetchall():
        rid = room[0]
        rows = db.execute('SELECT id,thread_id,kind,body FROM collaboration_messages WHERE room_id=? ORDER BY created,id', (rid,)).fetchall()
        known = {row[0]: row for row in rows}
        roots = {}
        def resolve(identifier, visited=None):
            visited = set() if visited is None else visited
            if identifier in roots:
                return roots[identifier]
            if identifier in visited or identifier not in known:
                return None
            visited.add(identifier)
            row = known[identifier]
            parent = row[1] if row[1] != identifier else None
            if row[2] == 'agent_result':
                body = json.loads(row[3])
                job = db.execute('SELECT context,goal_id FROM collaboration_jobs WHERE id=? AND room_id=?', (body.get('job_id', ''), rid)).fetchone()
                if job:
                    parent = json.loads(job[0]).get('origin_message_id')
                    if not parent and job[1]:
                        goal = db.execute('SELECT source_message_id FROM collaboration_goals WHERE id=? AND room_id=?', (job[1], rid)).fetchone()
                        parent = goal[0] if goal else None
            roots[identifier] = resolve(parent, visited) if parent else identifier
            return roots[identifier]
        maximum = db.execute('SELECT COALESCE(MAX(server_sequence),0) FROM collaboration_messages WHERE room_id=?', (rid,)).fetchone()[0]
        for row in rows:
            current = db.execute('SELECT server_sequence,thread_root_id FROM collaboration_messages WHERE id=?', (row[0],)).fetchone()
            if not current[0]:
                maximum += 1
                db.execute('UPDATE collaboration_messages SET server_sequence=? WHERE id=?', (maximum, row[0]))
            if not current[1]:
                root = resolve(row[0])
                if root:
                    mapped += 1
                else:
                    unresolved += 1
                db.execute('UPDATE collaboration_messages SET thread_root_id=? WHERE id=?', (root or row[0], row[0]))
        if 'chat_history_sequence' not in rooms:
            db.execute('UPDATE collaboration_rooms SET chat_history_sequence=? WHERE id=?', (maximum, rid))
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS collaboration_message_sequence ON collaboration_messages(room_id,server_sequence) WHERE server_sequence>0')
    db.execute("""CREATE TRIGGER IF NOT EXISTS collaboration_message_insert AFTER INSERT ON collaboration_messages
        BEGIN
            UPDATE collaboration_messages SET server_sequence=(SELECT COALESCE(MAX(server_sequence),0)+1 FROM collaboration_messages WHERE room_id=NEW.room_id AND id!=NEW.id),
                thread_root_id=CASE WHEN NEW.thread_root_id!='' THEN NEW.thread_root_id ELSE
                    COALESCE((SELECT NULLIF(thread_root_id,'') FROM collaboration_messages WHERE room_id=NEW.room_id AND id=NEW.thread_id AND id!=NEW.id),NEW.id) END
                WHERE id=NEW.id;
            INSERT INTO collaboration_room_changes(room_id,kind,object_id,version,created)
                VALUES (NEW.room_id,'message',NEW.id,NEW.version,NEW.created);
        END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS collaboration_message_change AFTER UPDATE OF version,state,body ON collaboration_messages
        WHEN NEW.version!=OLD.version OR NEW.state!=OLD.state OR NEW.body!=OLD.body
        BEGIN INSERT INTO collaboration_room_changes(room_id,kind,object_id,version,created)
            VALUES (NEW.room_id,'message',NEW.id,NEW.version,NEW.created); END""")
    for table, kind in [('collaboration_jobs', 'job'), ('collaboration_goals', 'goal')]:
        for operation in ('INSERT', 'UPDATE'):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS chatroom_{kind}_{operation.lower()} AFTER {operation} ON {table}
                BEGIN INSERT INTO collaboration_room_changes(room_id,kind,object_id,version,created)
                VALUES (NEW.room_id,'{kind}',NEW.id,NEW.version,NEW.updated); END""")
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS chatroom_delivery_{operation.lower()} AFTER {operation} ON mcp_event_deliveries
            WHEN EXISTS (SELECT 1 FROM mcp_event_outbox WHERE id=NEW.event_id AND name='codepier.collaboration.message_mentioned.v1'
                AND json_extract(data,'$.test')=0)
            BEGIN INSERT INTO collaboration_room_changes(room_id,kind,object_id,version,created)
                SELECT room_id,'message_delivery',json_extract(data,'$.message_id'),NEW.fence+1,NEW.created
                FROM mcp_event_outbox WHERE id=NEW.event_id; END""")
    if mapped or unresolved:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('chatroom_thread_backfill',?)",
                   (json.dumps({'mapped': mapped, 'unresolved': unresolved}),))
