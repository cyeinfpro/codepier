"""Transactional v8 multi-user migration; legacy credentials and keys stay intact.

The caller disables FK enforcement before BEGIN, validates foreign_key_check
before COMMIT and restores enforcement immediately. Rebuilds remove global alias
constraints, not resource IDs. No network or user admission occurs in migration.
"""
from __future__ import annotations

SCHEMA_VERSION = '8'

DDL = '''
CREATE TABLE IF NOT EXISTS membership_blocks(space_id TEXT NOT NULL REFERENCES spaces(id),user_id TEXT NOT NULL REFERENCES users(id),blocked INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(space_id,user_id));
CREATE TABLE IF NOT EXISTS space_invites(hash TEXT PRIMARY KEY,space_id TEXT NOT NULL REFERENCES spaces(id),level TEXT NOT NULL CHECK(level IN ('admin','member','guest')),created_by TEXT NOT NULL REFERENCES users(id),expires REAL NOT NULL,used_by TEXT,created REAL NOT NULL);

CREATE TABLE IF NOT EXISTS iam_users (
 user_id TEXT PRIMARY KEY REFERENCES users(id), active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 instance_admin INTEGER NOT NULL DEFAULT 0 CHECK(instance_admin IN (0,1)),
 local_login INTEGER NOT NULL DEFAULT 1 CHECK(local_login IN (0,1)),
 epoch INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1, display_name TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS spaces (
 id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('personal','team','legacy')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memberships (
 space_id TEXT NOT NULL REFERENCES spaces(id), user_id TEXT NOT NULL REFERENCES users(id),
 source TEXT NOT NULL DEFAULT 'manual', level TEXT NOT NULL CHECK(level IN ('owner','admin','member','guest')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), expires REAL,
 version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(space_id,user_id,source));
CREATE TABLE IF NOT EXISTS role_assignments (
 role_id TEXT NOT NULL REFERENCES access_roles(id), user_id TEXT NOT NULL REFERENCES users(id),
 space_id TEXT NOT NULL REFERENCES spaces(id), source TEXT NOT NULL DEFAULT 'manual',
 may_delegate INTEGER NOT NULL DEFAULT 0 CHECK(may_delegate IN (0,1)),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), expires REAL,
 version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(role_id,user_id,source));
CREATE TABLE IF NOT EXISTS session_security (
 session_hash TEXT PRIMARY KEY REFERENCES sessions(id_hash) ON DELETE CASCADE,
 epoch INTEGER NOT NULL, authenticated_at REAL NOT NULL, identity_id TEXT, provider_sid TEXT);
CREATE TABLE IF NOT EXISTS native_ownership (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
 owner_user_id TEXT NOT NULL REFERENCES users(id), project_id TEXT NOT NULL,
 device_id TEXT NOT NULL, created REAL NOT NULL, visibility TEXT NOT NULL DEFAULT 'user'
 CHECK(visibility IN ('user','space')));
CREATE TABLE IF NOT EXISTS native_upload_ownership (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
 user_id TEXT NOT NULL REFERENCES users(id), project_id TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oidc_providers (
 id TEXT PRIMARY KEY, label TEXT NOT NULL, issuer TEXT NOT NULL UNIQUE,
 client_id TEXT NOT NULL, client_secret TEXT NOT NULL, discovery_url TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 admission TEXT NOT NULL DEFAULT 'closed' CHECK(admission IN ('closed','jit')),
 group_claim TEXT NOT NULL DEFAULT 'groups', required_group TEXT NOT NULL DEFAULT '',
 scopes TEXT NOT NULL DEFAULT 'openid profile', endpoint_origins TEXT NOT NULL DEFAULT '[]',
 freshness_seconds INTEGER NOT NULL DEFAULT 900 CHECK(freshness_seconds BETWEEN 60 AND 86400),
 version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS external_identities (
 id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,
 provider_id TEXT NOT NULL REFERENCES oidc_providers(id), user_id TEXT NOT NULL REFERENCES users(id),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 disabled_reason TEXT NOT NULL DEFAULT '', groups_json TEXT NOT NULL DEFAULT '[]', checked_at REAL NOT NULL,
 fresh_until REAL NOT NULL, upstream_tokens TEXT, created REAL NOT NULL,
 UNIQUE(issuer,subject));
CREATE TABLE IF NOT EXISTS oidc_transactions (
 state_hash TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES oidc_providers(id),
 browser_hash TEXT NOT NULL, nonce TEXT NOT NULL, verifier TEXT NOT NULL,
 return_to TEXT NOT NULL, link_user_id TEXT, link_session_hash TEXT,
 provider_version INTEGER NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS oidc_logout_replays (
 provider_id TEXT NOT NULL REFERENCES oidc_providers(id), jti TEXT NOT NULL,
 expires REAL NOT NULL, PRIMARY KEY(provider_id,jti));
CREATE TABLE IF NOT EXISTS group_mappings (
 id TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES oidc_providers(id), group_name TEXT NOT NULL,
 space_id TEXT NOT NULL REFERENCES spaces(id), level TEXT NOT NULL DEFAULT 'member'
 CHECK(level IN ('admin','member','guest')), role_id TEXT REFERENCES access_roles(id),
 may_delegate INTEGER NOT NULL DEFAULT 0 CHECK(may_delegate IN (0,1)),
 version INTEGER NOT NULL DEFAULT 1, UNIQUE(provider_id,group_name,space_id,role_id));
CREATE TABLE IF NOT EXISTS iam_replays (
 user_id TEXT NOT NULL, space_id TEXT NOT NULL, action TEXT NOT NULL, idem TEXT NOT NULL,
 fingerprint TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(user_id,space_id,action,idem));
CREATE TABLE IF NOT EXISTS oidc_cache (
 provider_id TEXT PRIMARY KEY REFERENCES oidc_providers(id), version INTEGER NOT NULL,
 metadata TEXT NOT NULL, jwks TEXT NOT NULL, expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS memberships_user ON memberships(user_id,space_id);
CREATE INDEX IF NOT EXISTS assignments_user ON role_assignments(user_id,space_id);
CREATE INDEX IF NOT EXISTS identities_user ON external_identities(user_id);
'''


def _add(db, table, additions):
    columns = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
    for name, definition in additions.items():
        if name not in columns:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


def _rebuild(db, table, substitutions):
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    changed = sql
    for before, after in substitutions:
        changed = changed.replace(before, after)
    if changed == sql:
        return
    replacement = '__iam_v8_' + table
    changed = changed.replace('CREATE TABLE ' + table, 'CREATE TABLE ' + replacement, 1)
    changed = changed.replace('CREATE TABLE "' + table + '"', 'CREATE TABLE ' + replacement, 1)
    indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
    db.execute(changed)
    names = ','.join('"' + r[1] + '"' for r in db.execute(f'PRAGMA table_info({table})'))
    db.execute(f'INSERT INTO {replacement}({names}) SELECT {names} FROM {table}')
    db.execute(f'DROP TABLE {table}')
    db.execute(f'ALTER TABLE {replacement} RENAME TO {table}')
    for statement in indexes:
        db.execute(statement)


def migrate(db):
    if db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] == SCHEMA_VERSION:
        return
    for statement in DDL.split(';'):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR IGNORE INTO spaces(id,label,kind,created) VALUES('legacy','Personal / Legacy','legacy',strftime('%s','now'))")
    tables = ('devices', 'projects', 'vps_connections', 'access_roles', 'access_profiles',
              'grants', 'operations', 'artifacts', 'workflows', 'audit')
    for table in tables:
        _add(db, table, {'space_id': "TEXT NOT NULL DEFAULT 'legacy' REFERENCES spaces(id)",
                         'owner_user_id': 'TEXT REFERENCES users(id)'})
        db.execute(f'CREATE INDEX IF NOT EXISTS {table}_space ON {table}(space_id)')
    _add(db, 'grants', {'identity_id': 'TEXT REFERENCES external_identities(id)',
                         'user_epoch': 'INTEGER NOT NULL DEFAULT 1'})
    for table in ('operations', 'artifacts', 'workflows'):
        _add(db, table, {'visibility': "TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','space'))"})
    _add(db, 'oauth_requests', {'bound_user_id': 'TEXT', 'bound_session_hash': 'TEXT', 'space_id': 'TEXT'})
    _rebuild(db, 'projects', [('alias_key TEXT UNIQUE NOT NULL', 'alias_key TEXT NOT NULL')])
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS project_space_alias ON projects(space_id,alias_key)')
    _rebuild(db, 'vps_connections', [('name_key TEXT UNIQUE NOT NULL', 'name_key TEXT NOT NULL'),
                                    ('UNIQUE(host,port,username)', 'UNIQUE(space_id,host,port,username)')])
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS vps_space_name ON vps_connections(space_id,name_key)')
    _rebuild(db, 'access_roles', [('UNIQUE(user_id,label_key)', 'UNIQUE(space_id,label_key)'),
                                  ('UNIQUE(user_id,create_key)', 'UNIQUE(space_id,user_id,create_key)')])
    _rebuild(db, 'access_profiles', [('UNIQUE(user_id,label_key)', 'UNIQUE(space_id,user_id,label_key)'),
                                     ('UNIQUE(user_id,create_key)', 'UNIQUE(space_id,user_id,create_key)')])
    first = db.execute('SELECT id FROM users ORDER BY created,id LIMIT 1').fetchone()
    owner = first[0] if first else None
    # Existing local credentials retain login. Only the original bootstrap owner
    # receives instance authority; future users never inherit it from OIDC claims.
    for user in db.execute('SELECT id FROM users').fetchall():
        db.execute('INSERT OR IGNORE INTO iam_users(user_id,instance_admin) VALUES(?,?)', (user[0], int(user[0] == owner)))
        db.execute('INSERT OR IGNORE INTO memberships(space_id,user_id,level) VALUES(?,?,?)',
                   ('legacy', user[0], 'owner' if user[0] == owner else 'member'))
    if owner:
        for table in ('devices', 'projects', 'vps_connections'):
            db.execute(f'UPDATE {table} SET owner_user_id=? WHERE owner_user_id IS NULL AND space_id=\'legacy\'', (owner,))
        for table in ('access_roles', 'access_profiles', 'grants'):
            db.execute(f'UPDATE {table} SET owner_user_id=user_id WHERE owner_user_id IS NULL')
        for table in ('operations', 'artifacts', 'workflows', 'audit'):
            db.execute(f'''UPDATE {table} SET owner_user_id=COALESCE(
                (SELECT id FROM users WHERE 'panel:'||username={table}.actor),
                (SELECT user_id FROM grants WHERE 'mcp:'||grants.id||':'||grants.label={table}.actor),?)
                WHERE owner_user_id IS NULL AND space_id='legacy' ''', (owner,))
    db.execute('''INSERT OR IGNORE INTO session_security(session_hash,epoch,authenticated_at)
                  SELECT s.id_hash,u.epoch,0 FROM sessions s JOIN iam_users u ON u.user_id=s.user_id''')
    # bootstrap CLI/test-created local users remain compatible, but not new admin.
    db.execute('''CREATE TRIGGER IF NOT EXISTS iam_local_user AFTER INSERT ON users BEGIN
        INSERT OR IGNORE INTO iam_users(user_id,instance_admin)
          VALUES(NEW.id,CASE WHEN (SELECT count(*) FROM iam_users)=0 THEN 1 ELSE 0 END);
        INSERT OR IGNORE INTO memberships(space_id,user_id,level)
          VALUES('legacy',NEW.id,CASE WHEN (SELECT instance_admin FROM iam_users WHERE user_id=NEW.id)=1 THEN 'owner' ELSE 'member' END);
        END''')
    # Enforce cross-Space references even for a future mistakenly unscoped caller.
    relations = [('projects','device_id','devices'), ('access_profiles','role_id','access_roles'),
                 ('grants','profile_id','access_profiles'), ('grants','role_id','access_roles'),
                 ('operations','project_id','projects'), ('operations','device_id','devices'), ('operations','grant_id','grants'),
                 ('workflows','project_id','projects'), ('workflows','device_id','devices'), ('workflows','grant_id','grants'), ('artifacts','project_id','projects'), ('artifacts','device_id','devices'), ('artifacts','grant_id','grants'),
                 ('role_assignments','role_id','access_roles'), ('group_mappings','role_id','access_roles'), ('native_ownership','project_id','projects'), ('native_ownership','device_id','devices'), ('native_upload_ownership','project_id','projects')]
    for child, column, parent in relations:
        for event in ('INSERT', 'UPDATE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS iam_{child}_{column}_{event.lower()}
                BEFORE {event} ON {child} WHEN NEW.{column} IS NOT NULL AND EXISTS(
                  SELECT 1 FROM {parent} WHERE id=NEW.{column} AND space_id<>NEW.space_id)
                BEGIN SELECT RAISE(ABORT,'cross-space reference'); END''')
    for event in ('INSERT', 'UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS iam_vps_projects_{event.lower()} BEFORE {event} ON vps_projects
          WHEN (SELECT space_id FROM vps_connections WHERE id=NEW.vps_id)<>
               (SELECT space_id FROM projects WHERE id=NEW.project_id)
          BEGIN SELECT RAISE(ABORT,'cross-space VPS binding'); END''')
    for event in ('INSERT','UPDATE'):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS iam_created_projects_{event.lower()} BEFORE {event} ON role_created_projects
          WHEN (SELECT space_id FROM access_roles WHERE id=NEW.role_id)<>
               (SELECT space_id FROM projects WHERE id=NEW.project_id)
          BEGIN SELECT RAISE(ABORT,'cross-space created-project binding'); END""")
    db.execute("UPDATE meta SET value=? WHERE key='schema'", (SCHEMA_VERSION,))
